# -*- coding: utf-8 -*-
"""おたよりの下書きを作る。Vercel の Python ランタイムで動く。

キーは Vercel の環境変数に入れる。ブラウザには出さない。
  ANTHROPIC_API_KEY があれば Claude を使う（推奨）
  なければ OPENAI_API_KEY を使う
どちらも無いときは 503 を返し、アプリ側は今までの定型文に戻る。
"""
import os, json, base64
from http.server import BaseHTTPRequestHandler

MAX_BODY = 6 * 1024 * 1024      # 去年の資料の写真を数枚のせても足りる
MAX_IMAGES = 3

SYSTEM = """あなたは保育園の先生です。保護者に配るおたよりの下書きを書きます。

書き方のきまり:
- 一文を短くする。声に出して読める長さにする
- 難しい言葉を使わない。役所の言葉にしない
- 「つまり」「さらに」「重要なのは」は使わない
- 保護者をせかさない。命令しない
- 絵文字は使わない
- 園の「使わない言葉」は、形を変えたものも使わない
  （「がんばれ」がだめなら「がんばって」「がんばろう」も使わない）

かならず本文に入れるもの:
- 日にち、時間、場所。今年渡されたものをそのまま書く
- 行を分けて、ひと目で分かるように置く

去年のおたよりが渡されたときのあつかい:
- 書き出し、季節のあいさつ、しめくくりの言い回しは、その園の癖をまねる
- クラスごとの子どもの様子は、去年を参考にして今年のぶんを書いてよい
- 日にち・時間・場所は、今年渡されたものだけを書く

本文に書いてはいけないもの（園の約束ごと）:
持ちもの、雨のときの決まり、連絡の手段や名前、駐車場、集合時間、申し込みの期限、金額。
これらは今年ぶんを渡されたときだけ書く。渡されていないのに去年にあった場合は、
本文に入れず carried_over に書き出す。

出力は次のJSONだけ。前置きも説明も書かない:
{"letter":"おたよりの本文","carried_over":[{"item":"去年にあった約束ごと","ask":"先生に確認してほしいひとこと"}]}"""


def build_prompt(d):
    g, e = d.get("garden", {}), d.get("event", {})
    p = ["【園のこと】",
         f"園の名前: {g.get('name','')}",
         f"クラス: {g.get('classes','')}"]
    if g.get("principle"):
        p.append(f"大切にしていること: {g['principle']}")
    if g.get("ng"):
        p.append(f"使わない言葉: {g['ng']}")
    p += ["", "【今年の行事（これが事実）】",
          f"行事: {e.get('name','')}",
          f"日にち: {e.get('date','')}",
          f"時間: {e.get('time','')}から",
          f"場所: {e.get('place','')}",
          f"文のふんいき: {e.get('mood','')}"]
    if e.get("classes_note"):
        p.append(f"対象: {e['classes_note']}")
    if d.get("materials"):
        p += ["", "【去年の同じ行事のおたより】", "添付した写真がそれです。言い回しの参考にしてください。"]
    return "\n".join(p)


def split_data_url(u):
    """dataURL を (media_type, base64本体) に分ける。ダメなら None。"""
    if not isinstance(u, str) or not u.startswith("data:"):
        return None
    try:
        head, b64 = u.split(",", 1)
        mt = head[5:].split(";")[0]
        if mt not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            return None
        base64.b64decode(b64[:64] + "==")   # 形だけ確認
        return mt, b64
    except Exception:
        return None


def call_claude(d, key):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = []
    for u in (d.get("materials") or [])[:MAX_IMAGES]:
        s = split_data_url(u)
        if s:
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": s[0], "data": s[1]}})
    content.append({"type": "text", "text": build_prompt(d)})
    r = client.messages.create(
        model="claude-opus-5",
        max_tokens=4000,
        output_config={"effort": "medium"},
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    if r.stop_reason == "refusal":
        raise RuntimeError("refusal")
    return "".join(b.text for b in r.content if b.type == "text")


def call_openai(d, key):
    from openai import OpenAI
    client = OpenAI(api_key=key)
    content = []
    for u in (d.get("materials") or [])[:MAX_IMAGES]:
        if split_data_url(u):
            content.append({"type": "image_url", "image_url": {"url": u}})
    content.append({"type": "text", "text": build_prompt(d)})
    r = client.chat.completions.create(
        model="gpt-4.1", temperature=0.7,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": content}])
    return r.choices[0].message.content


def parse_out(text):
    """JSONで返ってくる前提。崩れていても本文だけは拾う。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        d = json.loads(t)
        letter = (d.get("letter") or "").strip()
        if letter:
            co = [c for c in (d.get("carried_over") or []) if isinstance(c, dict)]
            return {"letter": letter, "carried_over": co[:6]}
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(t[i:j + 1])
            if d.get("letter"):
                return {"letter": d["letter"].strip(),
                        "carried_over": (d.get("carried_over") or [])[:6]}
        except Exception:
            pass
    return {"letter": t, "carried_over": []} if t else None


def make_draft(d):
    ak, ok = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("OPENAI_API_KEY")
    if ak:
        return parse_out(call_claude(d, ak)), "claude"
    if ok:
        return parse_out(call_openai(d, ok)), "openai"
    return None, None


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        # つながっているかの確認用。キーそのものは返さない。
        self._send(200, {"ok": True,
                         "ai": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "写真が大きすぎます"})
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "読み取れませんでした"})
        if not (d.get("garden") or {}).get("name") or not (d.get("event") or {}).get("name"):
            return self._send(400, {"error": "園の名前と行事が要ります"})
        try:
            out, via = make_draft(d)
        except Exception as ex:
            return self._send(502, {"error": "生成できませんでした", "detail": str(ex)[:200]})
        if not out:
            return self._send(503, {"error": "AIがまだつながっていません"})
        out["via"] = via
        self._send(200, out)
