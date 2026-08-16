import os
import re
import shutil

INJECTED_CSS = """
<style>
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background-color: #0f1715 !important;
    color: #e2e8f0 !important;
    padding: 30px;
    line-height: 1.6;
}
h1, h2, h3, h4, .head-title { color: #2dd4bf !important; }
a { color: #2dd4bf !important; text-decoration: underline; }
div.unit_arti, section.arti {
    background-color: #14201d !important;
    border: 1px solid #1f2f2b !important;
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 20px;
    color: #e2e8f0 !important;
}
div.unit_arti:target, section.arti:target, div:target, a:target + div {
    border: 2px solid #2dd4bf !important;
    background-color: #1a2e29 !important;
    box-shadow: 0 0 20px rgba(45, 212, 191, 0.4);
}
</style>
"""

def sync_static_laws():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    src_dir = os.path.join(base_dir, "data", "pobrane_ustawy")
    dest_dir = os.path.join(base_dir, "static", "pobrane_ustawy")

    os.makedirs(dest_dir, exist_ok=True)

    if os.path.exists(src_dir):
        for f in os.listdir(src_dir):
            if f.endswith(".html"):
                src_file = os.path.join(src_dir, f)
                dest_file = os.path.join(dest_dir, f)
                with open(src_file, "r", encoding="utf-8") as rf:
                    content = rf.read()
                
                # Usuwamy niepotrzebne zapytania do /act.css i /act.js
                content = content.replace('href="/act.css"', '').replace('src="/act.js"', '')

                # Wstrzykujemy kotwice id="arti_X" oraz id="art_X" dla bezbłędnego przewijania przeglądarki
                content = re.sub(
                    r'(<[^>]*\bdata-id="(arti_[^"]+)"[^>]*>)',
                    r'<a id="\2" name="\2"></a>\1',
                    content
                )

                # Wstrzykujemy CSS stylizujący ciemne tło i podświetlenie wybranego artykułu
                if "<head>" in content:
                    content = content.replace("<head>", f"<head>{INJECTED_CSS}")
                else:
                    content = f"{INJECTED_CSS}\n{content}"

                with open(dest_file, "w", encoding="utf-8") as wf:
                    wf.write(content)
        print(f"✓ Pomyślnie zsynchronizowano, wstrzyknięto kotwice id oraz przestylizowano pliki HTML do {dest_dir}")



if __name__ == "__main__":
    sync_static_laws()
