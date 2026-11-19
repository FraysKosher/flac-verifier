"""Analyze every .flac in a directory natively; print {name: result} JSON.

Interpreter choice is made by parity.mjs: repo .venv, then $FV_PYTHON, then python3.
"""
import json
import os
import sys
import tempfile

WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(WEB))
sys.path.insert(0, WEB)

import engine_host  # noqa: E402


def main(directory):
    engine_host.setup(tempfile.mkdtemp(prefix="fv-parity-"))
    out = {}
    for name in sorted(os.listdir(directory)):
        if name.lower().endswith(".flac"):
            line, _png = engine_host.analyze(os.path.join(directory, name), "full", None)
            out[name] = json.loads(line)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main(sys.argv[1])
