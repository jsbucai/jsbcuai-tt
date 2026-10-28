"""Fetch a verified, project-local Gradle distribution without touching old tools."""
import hashlib
import os
import pathlib
import urllib.request
import zipfile

root = pathlib.Path(__file__).resolve().parents[1]
runtime = root / "runtime" / "toolchain"
runtime.mkdir(parents=True, exist_ok=True)
archive = runtime / "gradle-8.9-bin.zip"
# Pinned against https://gradle.org/release-checksums/ (8.9, binary-only).
# A mirror may change the transport, never the expected distribution content.
url = os.environ.get("UL_GRADLE_DOWNLOAD_URL", "https://downloads.gradle.org/distributions/gradle-8.9-bin.zip")
expected = "d725d707bfabd4dfdc958c624003b3c80accc03f7037b5122c4b1d0ef15cecab"
if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    urllib.request.urlretrieve(url, archive)
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected, "Gradle checksum mismatch"
if not (runtime / "gradle-8.9/bin/gradle").is_file():
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(runtime)
print("Verified Gradle 8.9:", runtime / "gradle-8.9")
