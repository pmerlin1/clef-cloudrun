"""Build the pinned upstream extension for the supported Cloud Run GPUs."""
import hashlib
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

URL = "https://files.pythonhosted.org/packages/f4/d1/eba27735f31bd5527d39a3c7f693e6ded7b844cab275db719a15ad37b6cd/causal_conv1d-1.7.0.tar.gz"
SHA256 = "3202758494eaa7b597ce1c282dfa188889506bfcb92cad3c407d26736bfcd32b"

with tempfile.TemporaryDirectory() as directory:
    archive = Path(directory) / "source.tar.gz"
    source = urllib.request.urlopen(URL, timeout=120).read()
    if hashlib.sha256(source).hexdigest() != SHA256:
        raise RuntimeError("causal-conv1d source checksum mismatch")
    archive.write_bytes(source)
    with tarfile.open(archive) as bundle:
        bundle.extractall(directory, filter="data")
    root = Path(directory) / "causal_conv1d-1.7.0"
    setup = root / "setup.py"
    text = setup.read_text()
    marker = "    # HACK: The compiler flag -D_GLIBCXX_USE_CXX11_ABI"
    if text.count(marker) != 1:
        raise RuntimeError("Upstream CUDA build layout changed")
    # Upstream hardcodes every architecture; TORCH_CUDA_ARCH_LIST cannot override
    # that list. Target only Ada L4 (sm_89) and Blackwell RTX PRO 6000
    # (sm_120), keeping all kernel code untouched.
    text = text.replace(
        marker,
        '    cc_flag = ["-gencode", "arch=compute_89,code=sm_89", '
        '"-gencode", "arch=compute_120,code=sm_120"]\n\n' + marker,
    )
    setup.write_text(text)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-cache-dir",
                    "--no-build-isolation", "--no-deps", str(root), "--wheel-dir=/wheels"], check=True)
