"""Reading Android's binary XML (AXML) — specifically an APK's AndroidManifest.xml.

Every fact worth auditing before an upload lives in the manifest: the package
name, the version code Meta compares against the channel, the target SDK floor,
whether the build is debuggable, whether it declares itself a VR app at all. All
of it is one `aapt2 dump` away — but aapt2 lives in the Android SDK build-tools,
and Visor's premise is that `adb` is the only host tool you need. So this reads
the format directly.

AXML is a chunked binary format: a length-prefixed header, a string pool, an
optional resource map, then a stream of start/end element chunks. It is stable —
it has to be, the platform parses it on every install — and small enough to read
in one pass. Documented in AOSP's `ResourceTypes.h`.

Only the parts a manifest uses are implemented. Styles, CDATA and the wider
`.arsc` resource table are skipped rather than half-supported.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from visor.adb.wrapper import VisorError

# Chunk types (ResourceTypes.h).
_RES_XML_TYPE = 0x0003
_RES_STRING_POOL_TYPE = 0x0001
_RES_XML_START_NAMESPACE = 0x0100
_RES_XML_END_NAMESPACE = 0x0101
_RES_XML_START_ELEMENT = 0x0102
_RES_XML_END_ELEMENT = 0x0103
_RES_XML_CDATA = 0x0104
_RES_XML_RESOURCE_MAP = 0x0180

# Typed value data types (Res_value::dataType).
_TYPE_NULL = 0x00
_TYPE_REFERENCE = 0x01
_TYPE_ATTRIBUTE = 0x02
_TYPE_STRING = 0x03
_TYPE_FLOAT = 0x04
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOLEAN = 0x12

_UTF8_FLAG = 1 << 8

_ANDROID_NS = "http://schemas.android.com/apk/res/android"

#: Public `android:` attribute resource IDs, for manifests that ship the
#: resource map but leave the attribute name strings empty. Only the attributes
#: a manifest audit actually asks about — the full table is thousands of rows
#: and lives in the platform, not here.
_ATTR_IDS = {
    0x01010000: "theme",
    0x01010001: "label",
    0x01010002: "icon",
    0x01010003: "name",
    0x01010006: "permission",
    0x0101000F: "debuggable",
    0x01010010: "exported",
    0x01010018: "authorities",
    0x0101001E: "screenOrientation",
    0x01010020: "configChanges",
    0x01010024: "value",
    0x01010025: "resource",
    0x0101020C: "minSdkVersion",
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x01010270: "targetSdkVersion",
    0x01010271: "maxSdkVersion",
    0x01010280: "allowBackup",
    0x01010281: "glEsVersion",
    0x0101028E: "required",
    0x010102D3: "hardwareAccelerated",
    0x01010572: "compileSdkVersion",
    0x010104EA: "extractNativeLibs",
    0x01010603: "roundIcon",
    0x0101055B: "installLocation",
}


class AxmlError(VisorError):
    """The bytes handed over are not a manifest this can read."""


def attribute_name_for_id(res_id: int) -> str:
    """Name for an `android:` attribute resource ID, or its hex if unknown.

    Returning the hex rather than raising keeps an unrecognised attribute
    visible in the parsed tree instead of vanishing from it — an audit that
    cannot name something should still be able to say it was there.
    """
    known = _ATTR_IDS.get(res_id)
    return known if known else f"0x{res_id:08x}"


@dataclass
class Element:
    """One manifest element, with its attributes and direct children."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list["Element"] = field(default_factory=list)

    def get(self, attribute: str, default: Any = None) -> Any:
        """Attribute value by bare name — `versionCode`, not `android:versionCode`."""
        return self.attributes.get(attribute, default)

    def iter(self) -> Iterator["Element"]:
        """This element and every descendant, depth first."""
        yield self
        for child in self.children:
            yield from child.iter()

    def findall(self, name: str) -> list["Element"]:
        """Every descendant with this tag name, at any depth."""
        return [e for e in self.iter() if e.name == name and e is not self]

    def find(self, name: str) -> Optional["Element"]:
        """The first descendant with this tag name, or None."""
        for element in self.iter():
            if element.name == name and element is not self:
                return element
        return None


def _read_string_pool(data: bytes, offset: int, size: int) -> list[str]:
    """Decode a RES_STRING_POOL chunk into a plain list of strings."""
    try:
        count, _styles, flags, strings_start, _styles_start = struct.unpack_from(
            "<IIIII", data, offset + 8
        )
    except struct.error as e:
        raise AxmlError(f"truncated string pool: {e}") from None

    utf8 = bool(flags & _UTF8_FLAG)
    index_at = offset + 28
    base = offset + strings_start
    strings: list[str] = []

    for i in range(count):
        try:
            (entry,) = struct.unpack_from("<I", data, index_at + 4 * i)
        except struct.error:
            raise AxmlError("string pool index runs past the end of the file") from None
        at = base + entry
        if at >= offset + size:
            raise AxmlError("string pool entry points outside its own chunk")
        strings.append(_decode_string(data, at, utf8))
    return strings


def _decode_length(data: bytes, at: int, wide: bool) -> tuple[int, int]:
    """Read a string pool length, which is 1 or 2 units with a high-bit escape."""
    if wide:
        (first,) = struct.unpack_from("<H", data, at)
        if first & 0x8000:
            (second,) = struct.unpack_from("<H", data, at + 2)
            return ((first & 0x7FFF) << 16) | second, at + 4
        return first, at + 2
    first = data[at]
    if first & 0x80:
        return ((first & 0x7F) << 8) | data[at + 1], at + 2
    return first, at + 1


def _decode_string(data: bytes, at: int, utf8: bool) -> str:
    if utf8:
        # UTF-8 entries carry both a character count and a byte count; only the
        # second one describes the bytes on disk.
        _chars, at = _decode_length(data, at, wide=False)
        length, at = _decode_length(data, at, wide=False)
        return data[at : at + length].decode("utf-8", errors="replace")
    length, at = _decode_length(data, at, wide=True)
    return data[at : at + length * 2].decode("utf-16-le", errors="replace")


def _pool(strings: list[str], index: int) -> str:
    """String pool lookup where -1 means "no string", as AXML spells null."""
    if index < 0 or index >= len(strings):
        return ""
    return strings[index]


def _value(
    strings: list[str], raw_value: int, data_type: int, data: int
) -> Any:
    """Turn a Res_value into the Python type a caller would expect."""
    if data_type == _TYPE_STRING:
        return _pool(strings, data if data != 0xFFFFFFFF else raw_value)
    if data_type == _TYPE_INT_BOOLEAN:
        return data != 0
    if data_type in (_TYPE_INT_DEC, _TYPE_INT_HEX):
        # Signed: -1 is a real manifest value (screenOrientation="unspecified").
        return struct.unpack("<i", struct.pack("<I", data))[0]
    if data_type == _TYPE_FLOAT:
        return struct.unpack("<f", struct.pack("<I", data))[0]
    if data_type == _TYPE_NULL:
        return None
    if data_type in (_TYPE_REFERENCE, _TYPE_ATTRIBUTE):
        # A reference into resources.arsc, which is not parsed here. Kept as a
        # marker so a caller can tell "points elsewhere" from "absent".
        return f"@0x{data:08x}"
    return data


def parse(data: bytes) -> Element:
    """Parse a compiled AndroidManifest.xml and return its root element."""
    if len(data) < 8:
        raise AxmlError("not a binary AndroidManifest.xml: too short to hold a header")
    chunk_type, header_size, total = struct.unpack_from("<HHI", data, 0)
    if chunk_type != _RES_XML_TYPE:
        hint = ""
        if data.lstrip()[:1] in (b"<", b"\xef"):
            hint = (
                " — this looks like source XML. The manifest inside an APK is "
                "compiled; extract AndroidManifest.xml from the .apk itself."
            )
        raise AxmlError(
            f"not a binary AndroidManifest.xml (chunk type 0x{chunk_type:04x}){hint}"
        )
    if total > len(data):
        raise AxmlError(
            f"truncated binary XML: header claims {total} bytes, got {len(data)}"
        )

    strings: list[str] = []
    resource_ids: list[int] = []
    root: Optional[Element] = None
    stack: list[Element] = []

    at = header_size
    while at + 8 <= total:
        chunk_type, chunk_header, chunk_size = struct.unpack_from("<HHI", data, at)
        if chunk_size < 8 or at + chunk_size > total:
            raise AxmlError(f"chunk at offset {at} has an impossible size {chunk_size}")

        if chunk_type == _RES_STRING_POOL_TYPE:
            strings = _read_string_pool(data, at, chunk_size)
        elif chunk_type == _RES_XML_RESOURCE_MAP:
            count = (chunk_size - chunk_header) // 4
            resource_ids = list(
                struct.unpack_from(f"<{count}I", data, at + chunk_header)
            )
        elif chunk_type == _RES_XML_START_ELEMENT:
            element = _read_start_element(data, at, strings, resource_ids)
            if stack:
                stack[-1].children.append(element)
            elif root is None:
                root = element
            stack.append(element)
        elif chunk_type == _RES_XML_END_ELEMENT:
            if stack:
                stack.pop()
        elif chunk_type in (
            _RES_XML_START_NAMESPACE,
            _RES_XML_END_NAMESPACE,
            _RES_XML_CDATA,
        ):
            pass  # Namespace prefixes and text nodes carry nothing an audit needs.

        at += chunk_size

    if root is None:
        raise AxmlError("binary XML contained no elements")
    return root


def _read_start_element(
    data: bytes, at: int, strings: list[str], resource_ids: list[int]
) -> Element:
    """Decode one RES_XML_START_ELEMENT chunk and its attribute array."""
    body = at + 16  # past ResChunk_header + lineNumber + comment
    try:
        _ns, name_index, attr_start, attr_size, attr_count = struct.unpack_from(
            "<IIHHH", data, body
        )
    except struct.error:
        raise AxmlError(f"truncated element header at offset {at}") from None

    element = Element(name=_pool(strings, name_index))
    for i in range(attr_count):
        entry = body + attr_start + i * attr_size
        try:
            ns_index, attr_name_index, raw_value, _size, _res0, data_type, value = (
                struct.unpack_from("<IIIHBBI", data, entry)
            )
        except struct.error:
            raise AxmlError(f"truncated attribute {i} of <{element.name}>") from None

        name = _pool(strings, attr_name_index)
        if not name and attr_name_index < len(resource_ids):
            # Name strings stripped; identity survives only in the resource map.
            name = attribute_name_for_id(resource_ids[attr_name_index])
        if not name:
            continue

        decoded = _value(strings, raw_value, data_type, value)
        # Bare names throughout: `android:versionCode` and a hypothetical
        # unprefixed `versionCode` never coexist in a real manifest, and making
        # callers spell the schema URL helps nobody.
        if _pool(strings, ns_index) in ("", _ANDROID_NS) or name not in element.attributes:
            element.attributes[name] = decoded
    return element
