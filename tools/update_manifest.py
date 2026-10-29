"""Regenerate the distribution manifest before publishing; --check for CI."""
import hashlib
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
paths = ['shellclaude.py', 'install.py', 'install.sh', 'requirements-chatgpt.txt', 'README.md']
paths += sorted(str(p.relative_to(root)) for directory in ('shellclaude_lib', 'defaults')
                for p in (root / directory).iterdir() if p.is_file() and p.suffix in ('.py', '.md', '.json'))
body = ''.join(hashlib.sha256((root / p).read_bytes()).hexdigest() + '  ' + p + '\n' for p in paths)
target = root / 'release-files.sha256'
if '--check' in sys.argv:
    if not target.exists() or target.read_text() != body:
        raise SystemExit('发布清单未更新：python3 tools/update_manifest.py')
else:
    target.write_text(body)
