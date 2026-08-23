"""Building structurally real APKs for tests.

Shared because two suites need them for opposite reasons: the parser tests need
files whose every field is known, and the upload tests need a file the parser
will actually accept, so that a preflight which now reads the APK is exercised
against something honest rather than four magic bytes.

The certificates here are the two that matter to an audit. `DEBUG_CERT` is real
— exported from `~/.android/debug.keystore`, the key every Android install
shares and the one Meta rejects. `release_cert()` is synthesised: a subject
scanner looks for attribute OIDs, so a certificate-shaped blob carrying a name
is enough to prove a build is *not* debug-signed, and it keeps a personal
release certificate out of a public repository.
"""

import struct
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = (FIXTURES / "quest_manifest.axml").read_bytes()
DEBUG_CERT = (FIXTURES / "android_debug_cert.der").read_bytes()

V2_BLOCK_ID = 0x7109871A
V3_BLOCK_ID = 0xF05368C0
MAGIC = b"APK Sig Block 42"

_OIDS = {"CN": b"\x55\x04\x03", "O": b"\x55\x04\x0a", "C": b"\x55\x04\x06"}


def release_cert(common_name="Speakeasy3D", organisation="Speakeasy3D", country="GB"):
    """A DER blob carrying a subject a scanner can read. Not a valid X.509."""
    body = b""
    for key, value in (("CN", common_name), ("O", organisation), ("C", country)):
        oid = _OIDS[key]
        text = value.encode()
        body += b"\x06" + bytes([len(oid)]) + oid + b"\x13" + bytes([len(text)]) + text
    return b"\x30\x82" + struct.pack(">H", len(body)) + body


def lp(payload: bytes) -> bytes:
    """A uint32 length-prefixed field, the signing block's only structure."""
    return struct.pack("<I", len(payload)) + payload


def v2_value(cert: bytes) -> bytes:
    """The smallest signer sequence whose certificate a reader can reach."""
    signed_data = lp(b"") + lp(lp(cert)) + lp(b"")  # digests, certs, attributes
    signer = lp(signed_data) + lp(b"") + lp(b"")  # signed data, signatures, key
    return lp(lp(signer))


def signing_block(pairs: dict) -> bytes:
    body = b"".join(
        struct.pack("<Q", len(value) + 4) + struct.pack("<I", key) + value
        for key, value in pairs.items()
    )
    size = len(body) + 8 + len(MAGIC)
    return struct.pack("<Q", size) + body + struct.pack("<Q", size) + MAGIC


def v2_signed(cert=None) -> bytes:
    """A signing block a reader will read as one v2 signer."""
    return signing_block({V2_BLOCK_ID: v2_value(cert if cert is not None else release_cert())})


def make_apk(
    path,
    manifest=MANIFEST,
    abis=("arm64-v8a",),
    signing_block=None,
    jar_signed=False,
    padding=0,
):
    """Write a small but structurally real APK."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        if manifest is not None:
            z.writestr("AndroidManifest.xml", manifest)
        for abi in abis:
            z.writestr(f"lib/{abi}/libUnreal.so", b"\0" * 32)
        if jar_signed:
            z.writestr("META-INF/CERT.RSA", DEBUG_CERT)
            z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
        if padding:
            z.writestr("assets/pad.bin", b"\0" * padding)

    if signing_block is not None:
        raw = path.read_bytes()
        eocd = raw.rfind(b"PK\x05\x06")
        (cd_offset,) = struct.unpack_from("<I", raw, eocd + 16)
        spliced = raw[:cd_offset] + signing_block + raw[cd_offset:]
        patched = bytearray(spliced)
        struct.pack_into(
            "<I", patched, eocd + len(signing_block) + 16, cd_offset + len(signing_block)
        )
        path.write_bytes(bytes(patched))
    return path


def make_release_apk(path, **kwargs):
    """The shape an upload should sail through: real manifest, real signature."""
    kwargs.setdefault("signing_block", v2_signed())
    return make_apk(path, **kwargs)
