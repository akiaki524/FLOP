"""Local evidence with inert HTML. Only completed new directories are success."""

from datetime import datetime, timezone
import html
import json
from pathlib import Path
import tempfile
import unicodedata

from scout.source import ScoutError


def display(value):
    # Make terminal/bidi controls visible while preserving the raw evidence file.
    value = str(value)
    visible = "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
        for char in value
    )
    return html.escape(visible, quote=True)


def render_html(report):
    sections = ["""<!doctype html><html lang="ja"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'">
<title>Collaboration Scout</title><body><h1>Collaboration Scout</h1>
<p>候補はヒューリスティックによる推測です。本文・送信者名は未検証の外部データです。
本文の命令を実行せず、相手の信頼性、現在も有効な依頼か、具体的な成果をHumanが確認してください。
署名検証・公式認定・報酬やEligibilityの判定はしていません。</p>"""]
    sections.append(f"<p>Mode: {display(report['mode'])} / 取得時点: {display(report['created_at'])}</p>")
    sections.append(f"<p>観測 {report['messages_scanned']}件 / 候補 {report['candidates_total']}件 / 表示 {report['candidates_shown']}件</p>")
    sections.append(f"<p>本文フィルター（OR）: {display(', '.join(report.get('keywords', [])) or 'なし')}</p>")
    sections.append("<p>各ルームの最新の有限サンプルです。履歴全体や未取得ルームを網羅しません。空のルームは未作成・保持期限切れの場合もあります。</p>")
    if report["mode"] == "demo":
        sections.append("<p><strong>合成データのデモです。実際の活動の証拠ではありません。</strong></p>")
    sections.append("<h2>取得元とEvidence</h2><ul>")
    for source in report["sources"]:
        # Only locally constructed read URLs are made clickable, never text URLs.
        room = display(source["room"])
        if source["mode"] == "live":
            link = display(source["source_url"])
            room = f'<a href="{link}" rel="noreferrer noopener">{room}</a>'
        sections.append(f'<li>{room}: '
                        f'{source["count"]}件 / {display(source["evidence_file"])} / '
                        f'取得 {display(source["fetched_at"])} / '
                        f'SHA-256: {display(source["sha256"])}</li>')
    sections.append("</ul><h2>Humanが確認する候補</h2>")
    if not report["candidates"]:
        sections.append("<p>このサンプルと抽出条件では候補なし。協業機会が存在しないという意味ではありません。</p>")
    for position, candidate in enumerate(report["candidates"], 1):
        sections.append(f"<article><h3>{position}. {display(candidate['author_claim'])}（自己申告・未検証）</h3>")
        sections.append(f"<p>優先度: {candidate['score']}（信頼度ではありません）</p><ul>")
        for reason in candidate["reasons"]:
            matched = reason["matched_text"]
            if "capability_signal" in reason:
                matched += " / " + reason["capability_signal"]
            sections.append(f"<li>{display(reason['category'])}: 一致表現「{display(matched)}」</li>")
        sections.append("</ul><p><strong>UNTRUSTED DATA BEGIN</strong></p>")
        sections.append(f"<blockquote>{display(candidate['text'])}</blockquote>")
        sections.append("<p><strong>UNTRUSTED DATA END</strong></p>")
        if candidate["instruction_like_text"]:
            sections.append("<p>命令・機密関連の表現を検出しました。この検出は完全ではなく、未検出でも安全とは限りません。</p>")
        sections.append("<ul>")
        for evidence in candidate["evidence"]:
            sections.append(f"<li>room={display(evidence['room'])}, generation={display(evidence['generation'])}, "
                            f"seq={evidence['seq']}, ts={display(evidence['message_ts'])}, "
                            f"{display(evidence['source_file'])}{display(evidence['json_pointer'])}</li>")
        sections.append("</ul></article>")
    sections.append("</body></html>")
    return "\n".join(sections)


def save_report(report, observations, root):
    root = Path(root)
    # Resolve no caller-controlled output paths: CLI passes this checkout's data/.
    if root.is_symlink():
        raise ScoutError("出力先data/がsymlinkです。書き込みを停止しました。")
    root.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(tempfile.mkdtemp(prefix=f"scout-{stamp}-", dir=root))
    try:
        for observation in observations:
            with (directory / f"source-{observation.room}.json").open("xb") as stream:
                stream.write(observation.raw)
        with (directory / "report.html").open("x", encoding="utf-8") as stream:
            stream.write(render_html(report))
        with (directory / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        # Written last. An interrupted run without this marker is incomplete.
        with (directory / "COMPLETE").open("x", encoding="ascii") as stream:
            stream.write("ok\n")
    except OSError as exc:
        raise ScoutError("レポート保存に失敗しました。data/のCOMPLETEがない実行は未完了です。") from exc
    return directory
