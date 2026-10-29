#!/usr/bin/env python3
"""cczh daemon — Claude Code 显示层翻译（英→中）。

工作方式：
  Claude Code 的 MessageDisplay 钩子把助手回复的每一批文本 POST 到
  /hooks/message-display，本服务调用 DeepSeek 翻成中文后返回 displayContent，
  Claude Code 用它替换屏幕上的这批文字。存档与模型上下文不受影响。

设计要点：
  - ``` / ~~~ 代码块整行原样放行，绝不送给翻译模型。
  - 已经是中文的批次直接放行（不调 API）。
  - 每个 message_id 维持"英文尾/中文尾"上下文，保证术语前后一致。
  - 翻译失败时返回空 JSON，Claude Code 自动回退显示英文原文。
"""
import json
import os
import re
import sys
import threading
import time
import http.client
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置

def _load_config():
    """读取配置。优先级: 环境变量 > config.json > 旧版 env 文件。"""
    conf = {}
    legacy = os.path.expanduser("~/.config/cczh/env")
    if os.path.exists(legacy):                       # 旧版 KEY=VALUE 文件
        try:
            for line in open(legacy, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
    path = os.path.expanduser("~/.config/cczh/config.json")
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
            if isinstance(data, dict):
                conf.update({k: v for k, v in data.items() if v not in (None, "")})
        except (OSError, ValueError):
            pass
    return conf

_CONF = _load_config()

def _pick(*names, default=None):
    """按序取配置：环境变量 > 旧版长名 > json 短名。"""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
        for k in (n, n.removeprefix("CCZH_").lower()):
            v = _CONF.get(k)
            if v not in (None, ""):
                return v
    return default

API_KEY  = _pick("CCZH_API_KEY", "DEEPSEEK_API_KEY", default="")
BASE_URL = _pick("CCZH_BASE_URL", default="https://api.deepseek.com")
MODEL    = _pick("CCZH_MODEL", default="deepseek-flash")
EFFORT   = _pick("CCZH_EFFORT", default="")          # 档位: low/high/max（thinking 开着时才有意义）
THINKING = _pick("CCZH_THINKING", default="off")     # 翻译不需要思考: off=彻底关闭
PORT     = int(_pick("CCZH_PORT", default="8795"))
TAIL_CHARS  = 280    # 送给译者的上下文长度
CHUNK_CHARS = 2000   # 超长批次按行边界切块
MAX_STATES  = 300    # 最多记住多少条消息的状态


def log(msg):
    sys.stderr.write(time.strftime("[%m-%d %H:%M:%S] ") + msg + "\n")

FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
CJK_RE   = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z]")
BOX_RE   = re.compile(r"[\u2500-\u257f]")

SYSTEM_PROMPT = """\
You translate streamed output of an AI coding assistant from English to Simplified Chinese,
for display to a Chinese developer.

Hard rules:
1. Return EXACTLY the same number of lines as the input. Translate line by line.
   Never merge, split, add, or drop lines. Blank lines stay blank.
2. Keep markdown structure intact: headings, bullets, numbering, bold, tables, links.
3. Keep verbatim and untranslated: inline code (`...`), file paths, commands, URLs,
   identifiers, log lines, and product/tool names (git, Docker, Rust, ...).
4. Tech terms may stay in the English form most natural for Chinese developers
   (token, hook, commit, agent, prompt ...).
5. Output ONLY the translated text. No preface, no quotes, no code fences around it."""

ASK_PROMPT = """\
You translate UI strings of an AI coding assistant from English to Simplified Chinese.
Input: numbered lines, each formatted as 'N|text'. Translate only the text after the marker.

Rules:
1. Keep the leading 'N|' marker and the line count exactly: one output line per input line.
2. Keep code, file paths, commands, URLs, identifiers, and product names verbatim.
3. Keep translations concise — these are dialog questions, short headers, and option labels.
4. Output ONLY the numbered lines."""

# ---------------------------------------------------------------- 统计与状态

_stats = {"batches": 0, "translated": 0, "passthrough": 0, "errors": 0,
          "total_ms": 0, "started": time.time(), "last_error": ""}
_stats_lock = threading.Lock()

def stat(key, val=1):
    with _stats_lock:
        if key == "errors":
            _stats["errors"] += 1
            _stats["last_error"] = str(val)
        else:
            _stats[key] += val

_states = {}   # message_id -> {"fence": bool, "en_tail": str, "zh_tail": str, "ts": float}
_states_lock = threading.Lock()

def get_state(message_id):
    with _states_lock:
        st = _states.get(message_id)
        if st is None:
            if len(_states) >= MAX_STATES:
                now = time.time()
                for k in sorted(_states, key=lambda k: _states[k]["ts"])[: len(_states) - MAX_STATES + 1]:
                    _states.pop(k, None)
            st = {"fence": False, "en_tail": "", "zh_tail": "", "ts": time.time()}
            _states[message_id] = st
        st["ts"] = time.time()
        return st

# ---------------------------------------------------------------- DeepSeek 客户端

class DeepSeek:
    """带连接池的极简 OpenAI 兼容客户端（仅 stdlib）。

    - 最多 4 条并发连接，支持多块并行翻译
    - 网络错误/5xx 重试一次；4xx（如 401/402/429）立即失败，不浪费时间
    - deadline: 整个请求的时间预算，超了就放弃（让 Claude Code 回退英文）
    """

    def __init__(self, base_url, api_key, model):
        from urllib.parse import urlsplit
        u = urlsplit(base_url if "://" in base_url else "https://" + base_url)
        self.host = u.netloc
        self.tls = u.scheme != "http"
        self.prefix = u.path.rstrip("/")
        self.path = self.prefix + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self._pool = []
        self._pool_lock = threading.Lock()
        self._sem = threading.Semaphore(4)

    def _acquire(self):
        self._sem.acquire()
        with self._pool_lock:
            if self._pool:
                conn, idle_since = self._pool.pop()
                if time.time() - idle_since > 30:   # 空闲连接可能已被服务端断开，不赌
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    return conn
            return self._new_conn()

    def _release(self, conn, healthy):
        if healthy:
            with self._pool_lock:
                self._pool.append((conn, time.time()))
        else:
            try:
                conn.close()
            except Exception:
                pass
        self._sem.release()

    def _new_conn(self):
        cls = http.client.HTTPSConnection if self.tls else http.client.HTTPConnection
        return cls(self.host, timeout=12)

    def chat(self, user_text, deadline=None):
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        }
        if THINKING == "off":                        # 翻译不需要推理：两种方言都发，彻底关掉
            payload["thinking"] = {"type": "disabled"}
            payload["reasoning_effort"] = "none"
        elif EFFORT:
            payload["effort"] = EFFORT
        body = json.dumps(payload).encode("utf-8")

        last_err = None
        for attempt in (0, 1, 2):                    # 连接类错误最多试 3 次（防闲置竞态）
            if deadline is not None and time.time() > deadline:
                raise RuntimeError(f"超时预算用尽: {last_err or ''}")
            conn = self._acquire()
            healthy = False
            try:
                conn.request("POST", self.path, body=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                })
                resp = conn.getresponse()
                data = resp.read()
                if resp.status == 200:
                    healthy = True
                    out = json.loads(data)["choices"][0]["message"]["content"]
                    return self._clean(out)
                last_err = RuntimeError(f"HTTP {resp.status}: {data[:200]!r}")
                if resp.status < 500:                # 4xx 重试无意义
                    raise last_err
            except (http.client.HTTPException, OSError) as e:
                last_err = e
            finally:
                self._release(conn, healthy)
        raise RuntimeError(f"翻译 API 调用失败: {last_err}")

    @staticmethod
    def _clean(text):
        t = text.strip()
        if t.startswith("```") and t.endswith("```"):  # 偶发：模型给整段加了围栏
            lines = t.splitlines()
            if len(lines) >= 2:
                t = "\n".join(lines[1:-1])
        for prefix in ("翻译：", "翻译:", "Translation:", "译文：", "译文:"):
            if t.startswith(prefix):
                t = t[len(prefix):]
        return t

_client = DeepSeek(BASE_URL, API_KEY, MODEL)

# ---------------------------------------------------------------- 文本处理

def looks_chinese(text):
    cjk = len(CJK_RE.findall(text))
    letters = len(LATIN_RE.findall(text))
    return cjk > 0 and cjk >= letters * 0.5

def has_enough_prose(text):
    """去掉代码段/网址/数字后是否还剩足够的英文单词。"""
    t = re.sub(r"`[^`]*`", " ", text)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[A-Za-z]*\d[A-Za-z0-9_.:/-]*", " ", t)   # 形如 v2.1、x86_64 的词
    return len(re.findall(r"[A-Za-z]{2,}", t)) >= 1

def classify(delta, fence):
    """把一批文本按行分类。返回 (items, fence)，item = (kind, line)。kind:
    fence=围栏行  code=代码行  pass=ASCII图放行  prose=进翻译段
    空行和纯符号行归入 prose 段一起送翻（模型被要求原样保留），
    这样多段落/表格批次只发一次 API。"""
    items = []
    for line in delta.splitlines(keepends=True):
        if FENCE_RE.match(line):
            fence = not fence
            items.append(("fence", line))
        elif fence:
            items.append(("code", line))
        elif BOX_RE.search(line):
            items.append(("pass", line))
        else:
            items.append(("prose", line))
    return items, fence

def translate_text(text, st, deadline=None):
    """翻译一段不含空行的文本（一个段落），支持切块并行。"""
    chunks = [text]
    if len(text) > CHUNK_CHARS:                      # 按行边界切块
        chunks, buf = [], ""
        for line in text.splitlines(keepends=True):
            if buf and len(buf) + len(line) > CHUNK_CHARS:
                chunks.append(buf)
                buf = line
            else:
                buf += line
        if buf:
            chunks.append(buf)

    def do(chunk):
        parts = []
        if st["en_tail"]:
            parts.append(f"[Previous English context]\n{st['en_tail']}")
        if st["zh_tail"]:
            parts.append(f"[Previous Chinese translation]\n{st['zh_tail']}")
        parts.append("Translate the lines below:\n" + chunk.rstrip("\n"))
        t0 = time.time()
        out = _client.chat("\n\n".join(parts), deadline=deadline)
        stat("total_ms", int((time.time() - t0) * 1000))
        return out

    if len(chunks) == 1:
        outs = [do(chunks[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
            outs = list(ex.map(do, chunks))

    translated = "\n".join(outs)
    body = text.rstrip("\n")
    n_in = body.count("\n") + 1
    n_out = translated.rstrip("\n").count("\n") + 1
    if n_out != n_in:                                # 行数不对：保底拼接，不丢内容
        log(f"line mismatch in={n_in} out={n_out}")
    return translated


def translate_run(run_lines, st, deadline=None):
    """翻译一段连续的 prose 行。

    空行是天然段落边界：空行本身不送翻、位置原样回填，
    这样模型吞行也只会影响单个段落，段落结构永不丢。"""
    en = "".join(run_lines)
    trail = "\n" if en.endswith("\n") else ""
    out, para = [], []

    def flush():
        if not para:
            return
        out.append(translate_text("\n".join(para), st, deadline).rstrip("\n"))
        para.clear()

    for line in en.rstrip("\n").split("\n"):
        if line.strip():
            para.append(line)
        else:
            flush()
            out.append(line)
    flush()
    zh = "\n".join(out)
    st["en_tail"] = en[-TAIL_CHARS:]
    st["zh_tail"] = zh[-TAIL_CHARS:]
    return zh + trail

def handle_display(payload):
    """MessageDisplay 主流程，返回 displayContent 字符串。"""
    message_id = payload.get("message_id", "?")
    delta = payload.get("delta", "")
    st = get_state(message_id)
    deadline = time.time() + 17                       # 硬预算，留 3s 余量给钩子的 20s

    if not API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置 (~/.config/cczh/env)")

    items, st["fence"] = classify(delta, st["fence"])
    prose = [l for kind, l in items if kind == "prose"]

    if not prose:                                     # 纯代码/符号批次
        stat("passthrough")
        return delta

    prose_text = "".join(prose)
    if looks_chinese(prose_text) or not has_enough_prose(prose_text):
        stat("passthrough")
        st["en_tail"], st["zh_tail"] = "", ""         # 语言切换了，重置上下文
        return delta

    # 连续 prose 段分组，逐段翻译（段间夹着的代码行原样保留在原位置）
    out_lines, run = [], []
    for kind, line in items:
        if kind == "prose":
            run.append(line)
        else:
            if run:
                out_lines.append(translate_run(run, st, deadline).rstrip("\n"))
                run = []
            out_lines.append(line.rstrip("\n"))
    if run:
        out_lines.append(translate_run(run, st, deadline).rstrip("\n"))

    stat("translated")
    return "\n".join(out_lines) + ("\n" if delta.endswith("\n") else "")

def handle_ask(payload):
    """PreToolUse(AskUserQuestion) 主流程：翻译问题/标题/选项，返回 updatedInput。

    弹窗是独立渲染路径、不走 MessageDisplay，所以在工具执行前改写参数。
    任何失败返回 {}，Claude Code 按原始英文提问，行为不变。"""
    ti = payload.get("tool_input") or {}
    questions = ti.get("questions") or []
    if not isinstance(questions, list) or not questions:
        return {}
    deadline = time.time() + 17

    for q in questions:
        if not isinstance(q, dict):
            continue
        fields = []
        for key in ("question", "header"):
            if q.get(key):
                fields.append((q, key, q[key]))
        for opt in q.get("options") or []:
            if not isinstance(opt, dict):
                continue
            for key in ("label", "description"):
                if opt.get(key):
                    fields.append((opt, key, opt[key]))
        if not fields:
            continue
        texts = [f[2] for f in fields]
        if all(looks_chinese(t) or not has_enough_prose(t) for t in texts):
            continue                                  # 整题已中文/无英文，跳过
        numbered = "\n".join(f"{i + 1}|{t}" for i, t in enumerate(texts))
        try:
            out = _client.chat(ASK_PROMPT + "\n\n" + numbered, deadline=deadline)
        except Exception as e:
            log(f"ask error: {e}")
            return {}
        parsed = {}
        for line in out.splitlines():
            m = re.match(r"^(\d+)\|(.*)$", line.strip())
            if m:
                parsed[int(m.group(1))] = m.group(2).strip()
        if len(parsed) != len(texts):
            log(f"ask markers missing: got {len(parsed)}/{len(texts)}")
            continue                                  # 解析不完整：这题保持原文
        for i, (obj, key, _) in enumerate(fields, 1):
            if parsed[i]:
                obj[key] = parsed[i]
        stat("translated")

    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "updatedInput": ti}}


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):                # 静默访问日志，错误仍写 stderr
        pass

    def _reply(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # Claude Code 等超时先走了，正常回退为显示英文

    def do_GET(self):
        if self.path == "/health":
            with _stats_lock:
                up = time.time() - _stats["started"]
            self._reply({"ok": True, "uptime_s": round(up), "model": MODEL})
        elif self.path == "/stats":
            with _stats_lock:
                s = dict(_stats)
            n = max(s["translated"], 1)
            s["avg_ms"] = s["total_ms"] // n
            self._reply(s)
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/hooks/ask-question":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self._reply(handle_ask(payload))
            except Exception as e:
                stat("errors", str(e))
                log(f"ask error: {e}")
                self._reply({})
            return
        if self.path != "/hooks/message-display":
            self._reply({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            display = handle_display(payload)
            stat("batches")
            self._reply({"hookSpecificOutput": {
                "hookEventName": "MessageDisplay",
                "displayContent": display,
            }})
        except Exception as e:                        # 任何失败：返回空 JSON → 显示英文原文
            stat("errors", str(e))
            log(f"error: {e}")
            self._reply({})

def serve():
    if not API_KEY:
        log("警告: 未配置 API key (CCZH_API_KEY / DEEPSEEK_API_KEY)")
    try:                                             # Windows/手动模式用 pid 文件管理进程
        pid_dir = os.path.dirname(os.path.expanduser("~/.config/cczh/."))
        os.makedirs(pid_dir, exist_ok=True)
        with open(os.path.expanduser("~/.config/cczh/cczh.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"listening on 127.0.0.1:{PORT} model={MODEL}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    serve()
