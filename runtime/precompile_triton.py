"""Stage NVIDIA's pinned driver link stub for CPU-only CudaUtils compilation.

The stub stays in /build and is NEVER copied into the runtime image. A real GPU
driver is still required at startup. CudaUtils only imports the compiled wrapper
during the build; no CUDA function is invoked.
"""

import hashlib
import io
import subprocess
import tarfile
import urllib.request
from pathlib import Path

URL = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-driver-dev-12-8_12.8.90-1_amd64.deb"
SHA256 = "5ffbd709ee3ed8dee4d82a07de6c789efd4b37c3b23e72f59e0c519c1701a692"


def main():
    archive = Path("/build/cuda-driver.deb")
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(29125)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError("CUDA driver stub checksum mismatch")
    archive.write_bytes(data)
    names = subprocess.check_output(["ar", "t", str(archive)], text=True).splitlines()
    member = next(name for name in names if name.startswith("data.tar."))
    tar_data = subprocess.check_output(["ar", "p", str(archive), member])
    with tarfile.open(fileobj=io.BytesIO(tar_data)) as bundle:
        library = next(member for member in bundle.getmembers() if member.isfile() and member.name.endswith("/stubs/libcuda.so"))
        destination = Path("/build/cuda-stub")
        destination.mkdir(exist_ok=True)
        (destination / "libcuda.so.1").write_bytes(bundle.extractfile(library).read())
    Path("/opt/triton-cache").mkdir(exist_ok=True)


if __name__ == "__main__":
    main()
