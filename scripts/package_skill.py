"""Build an allowlisted skill archive without drawings, reports or Git metadata."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import argparse

ROOT = Path(__file__).resolve().parents[1]

def build(destination):
    destination = Path(destination).resolve()
    files = [ROOT / p for p in ("SKILL.md", "README.md", "LICENSE", "requirements.txt", "docs/RELEASE-READINESS.md", "docs/demo-stamp.png", "docs/EDU-ACCEPTANCE-2026-11-25.md", "docs/edu-acceptance-before.png", "docs/edu-acceptance-after.png", "docs/XREF-ACCEPTANCE-2026-11-25.md")]
    for directory, pattern in (("agents", "*.yaml"), ("references", "*.md"), ("scripts", "*.py")):
        files.extend(sorted((ROOT / directory).glob(pattern)))
    if destination in files:
        raise ValueError("Archive must not replace a source file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for source in files:
            archive.write(source, "cad-dwg-doctor/" + source.relative_to(ROOT).as_posix())
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "dist" / "cad-dwg-doctor.zip"))
    print(build(parser.parse_args().out))
