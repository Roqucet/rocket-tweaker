#!/usr/bin/env python3

"""
Author: Rocket (Discord: @roqucet)
Created: 2026-01-27
Version: v0.2.1
Description: Gives more freedom for editing TTP:R .level/.episode files.
    Lets you dump a file to .json for manual editing, or create a .level/.episode from .json.
    Will save a backup when trying to overwrite a file
"""

import argparse
import base64
import io
import json
import os
import shutil
import struct
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class GameVersion(IntEnum):
    Unknown = -1
    Reawakened = 1
    Talos2 = 2
    Talos3 = 3


class BinaryReader:
    def __init__(self, stream):
        self.stream = stream

    def read_u8(self):
        return struct.unpack("<B", self.stream.read(1))[0]

    def read_u32(self):
        return struct.unpack("<I", self.stream.read(4))[0]

    def read_s32(self):
        return struct.unpack("<i", self.stream.read(4))[0]

    def read_f32(self):
        return struct.unpack("<f", self.stream.read(4))[0]

    def read_f64(self):
        return struct.unpack("<d", self.stream.read(8))[0]

    def read_data(self, size):
        return self.stream.read(size)

    def read_string(self):
        string_length = self.read_s32()

        # Unicode is identifed as a negative length
        encoding = "ascii"
        if string_length < 0:
            encoding = "utf-16"
            string_length *= -2

        data = self.read_data(string_length)
        return data.decode(encoding=encoding).rstrip("\x00")

    def peek(self, size):
        return self.stream.peek(size)[:size]


class BinaryWriter:
    def __init__(self, stream):
        self.stream = stream

    def write_u8(self, data):
        self.stream.write(struct.pack("<B", data))

    def write_u32(self, data):
        self.stream.write(struct.pack("<I", data))

    def write_s32(self, data):
        self.stream.write(struct.pack("<i", data))

    def write_f32(self, data):
        self.stream.write(struct.pack("<f", data))

    def write_f64(self, data):
        self.stream.write(struct.pack("<d", data))

    def write_data(self, data):
        self.stream.write(data)

    def write_string(self, data):
        encoding = "ascii"
        # Check is the string can be encoded as ascii
        if not data.isascii():
            encoding = "utf-16"
        bytes_data = data.encode(encoding=encoding)
        string_length = len(bytes_data)
        if len(bytes_data) > 0:
            string_length += 1  # + 1 for the null byte (but only if there are bytes)
        if not data.isascii():
            # -2 for removed utf-16 marker & +2 for extra null bytes cancel out
            string_length >>= 1
            string_length *= -1
            # Remove utf-16 marker (b'\xff\xfe') as Talos doesn't use it
            bytes_data = bytes_data[2:]
        bytes_data = bytes_data.ljust(
            string_length if string_length >= 0 else string_length * -2, b"\x00"
        )
        self.write_s32(string_length)
        self.stream.write(bytes_data)


@dataclass
class DecodeContext:
    game_version: GameVersion = GameVersion.Unknown
    script_path_cache: list = field(default_factory=list)
    database_path_cache: list = field(default_factory=list)
    asset_path_cache: list = field(default_factory=list)


class BaseObject(ABC):
    @abstractmethod
    def __init__(self):
        raise NotImplementedError

    def __repr__(self):
        """Common function to print object data"""
        ret = []
        for k, v in self.__dict__.items():
            ret.append(f"{k}: {v}")
        return "<" + ", ".join(ret) + ">"

    @classmethod
    @abstractmethod
    def parse(cls, reader, decode_context):
        """Create an object from a level file byte stream"""
        raise NotImplementedError

    @abstractmethod
    def to_dict(self):
        """Convert object to a dictionary for serialization"""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_dict(cls, dictionary):
        """Create an object from a deserialized dictionary"""
        raise NotImplementedError

    @abstractmethod
    def unparse(self, writer, decode_context):
        """Write an object to a level file byte stream"""
        raise NotImplementedError


class CommonHeader(BaseObject):
    def __init__(self, strings, guid):
        self.strings = strings
        self.guid = guid

    # Override __bool__ so we return false if there's no strings or guid
    def __bool__(self):
        return bool(self.strings or self.guid)

    @classmethod
    def create_empty(cls):
        return cls([], None)

    @classmethod
    def _parseR(cls, reader, optional_guid=True):
        strings = []
        while True:
            string_exists = reader.read_u32()
            if string_exists == 0:
                break
            strings.append(reader.read_string())
        reader.read_u32()  # property_length - Ignore as we always recalculate
        guid = None
        if optional_guid:
            has_guid = reader.read_u8()
            # TODO: find a case where this is true
            # Should fail a level -> JSON -> level test as it isn't unparsed
            if has_guid != 0:
                guid = reader.read_data(16)
        return cls(strings, guid)

    @classmethod
    def _parseT2(cls, reader, optional_guid=True):
        reader.read_u32()  # property_length - Ignore as we always recalculate
        strings = []
        while True:
            string_exists = reader.read_u32()
            if string_exists == 0:
                break
            strings.append(reader.read_string())
        guid = None
        if optional_guid:
            has_guid = reader.read_u8()
            # TODO: find a case where this is true
            # Should fail a level -> JSON -> level test as it isn't unparsed
            if has_guid != 0:
                guid = reader.read_data(16)
        return cls(strings, guid)

    @classmethod
    def parse(cls, reader, decode_context, optional_guid=True):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, optional_guid)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, optional_guid)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        if self.strings:
            ret.update({"strings": self.strings})
        if self.guid:
            ret.update({"guid": self.guid})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        strings = dictionary.get("strings", [])
        guid = dictionary.get("guid", None)
        return cls(strings, guid)

    def _unparseR(self, writer, byte_count, optional_guid=True):
        for string in self.strings:
            writer.write_u32(1)
            writer.write_string(string)
        writer.write_data(b"\x00" * 4)
        writer.write_u32(byte_count)

        # TODO: find a case where this is true
        # Should fail a level -> JSON -> level test as it isn't unparsed
        if optional_guid:
            writer.write_data(b"\x00" * 1)

    def _unparseT2(self, writer, byte_count, optional_guid=True):
        writer.write_u32(byte_count)

        for string in self.strings:
            writer.write_u32(1)
            writer.write_string(string)
        writer.write_data(b"\x00" * 4)

        # TODO: find a case where this is true
        # Should fail a level -> JSON -> level test as it isn't unparsed
        if optional_guid:
            writer.write_data(b"\x00" * 1)

    def unparse(self, writer, decode_context, byte_count, optional_guid=True):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, byte_count, optional_guid)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, byte_count, optional_guid)
        else:
            raise NotImplementedError("Unknown game version")


class ArrayOfBytes:
    def __init__(self, data):
        self.data = data

    def __repr__(self):
        return "<" + base64.b64encode(self.data).decode() + ">"

    def to_dict(self):
        return {"data": base64.b64encode(self.data).decode()}

    @classmethod
    def from_dict(cls, dictionary):
        return cls(base64.b64decode(dictionary["data"]))


class ArrayProperty(BaseObject):
    def __init__(
        self, unknown, element_type, include_type_header, header_data, elements
    ):
        self.unknown = unknown
        self.element_type = element_type
        self.include_type_header = include_type_header
        self.header_data = header_data
        self.elements = elements

    @classmethod
    def _parseR(cls, reader, decode_context):
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")
        element_type = reader.read_string()
        include_type_header = reader.read_u32()
        header_data = None
        if include_type_header != 0:
            if element_type == "EnumProperty":
                # We can call parse_separate_header since we know the type
                header_data = EnumProperty.parse_separate_header(reader, decode_context)
            elif element_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                header_data = StructProperty.parse_separate_header(
                    reader, decode_context, magic=include_type_header
                )
            else:
                raise NotImplementedError(
                    f'Unknown array type with extra data! Type:"{element_type}"'
                )

        reader.read_u32()  # Byte count - Ignore as we always recalculate
        reader.read_data(1)  # Unknown
        length = reader.read_u32()  # Don't save length as we always recalculate

        elements = []
        if element_type == "ByteProperty":  # Hacky ByteProperty fix
            # The ByteProperty type is weird and actually reads strings when part of enums
            # Use ArrayOfBytes instead
            elements.append(ArrayOfBytes(reader.read_data(length)))
        elif element_type in property_string_to_class:
            element_class = property_string_to_class[element_type]
            if not element_class in TESTED_ARRAY_CLASSES:
                raise NotImplementedError(
                    f'Untested array element type "{element_type}"'
                )
            for _ in range(length):
                elements.append(
                    element_class.parse(
                        reader,
                        decode_context,
                        include_header=False,
                        header_data=header_data,
                    )
                )
        else:
            raise NotImplementedError(
                f'Unimplemented array property type!: "{element_type}"'
            )

        return cls(
            non_zero_unknown, element_type, include_type_header, header_data, elements
        )

    @classmethod
    def _parseT2(cls, reader, decode_context):
        reader.read_u32()  # Byte count - Ignore as we always recalculate
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")
        element_type = reader.read_string()

        reader.read_data(1)  # Unknown
        length = reader.read_u32()  # Don't save length as we always recalculate

        header_data = None
        elements = []
        if element_type == "ByteProperty":  # Hacky ByteProperty fix
            # The ByteProperty type is weird and actually reads strings when part of enums
            # Use ArrayOfBytes instead
            elements.append(ArrayOfBytes(reader.read_data(length)))
        elif element_type in property_string_to_class:
            element_class = property_string_to_class[element_type]
            if not element_class in TESTED_ARRAY_CLASSES:
                raise NotImplementedError(
                    f'Untested array element type "{element_type}"'
                )

            if element_class == StructProperty:
                # We can call parse_separate_header since we know the type
                header_data = StructProperty.parse_separate_header(
                    reader, decode_context, magic=None
                )
            for _ in range(length):
                elements.append(
                    element_class.parse(
                        reader,
                        decode_context,
                        include_header=False,
                        header_data=header_data,
                    )
                )
        else:
            raise NotImplementedError(
                f'Unimplemented array property type!: "{element_type}"'
            )

        return cls(non_zero_unknown, element_type, None, header_data, elements)

    @classmethod
    def parse(cls, reader, decode_context):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, decode_context)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, decode_context)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        ret.update({"unknown": self.unknown})
        ret.update({"element_type": self.element_type})
        if self.include_type_header:
            ret.update({"include_type_header": self.include_type_header})
        if self.header_data:
            ret.update({"header_data": self.header_data})
        if self.elements:
            elements = []
            for element in self.elements:
                elements.append(element.to_dict())
            ret.update({"elements": elements})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        unknown = dictionary["unknown"]
        element_type = dictionary["element_type"]
        include_type_header = dictionary.get("include_type_header", 0)
        header_data = dictionary.get("header_data", None)
        elements = []
        if "elements" in dictionary:
            if element_type == "ByteProperty":  # Hacky ByteProperty fix
                # The ByteProperty type is weird and actually reads strings when part of enums
                # Use ArrayOfBytes instead
                elements.append(ArrayOfBytes.from_dict(dictionary["elements"][0]))
            elif element_type in property_string_to_class:
                element_class = property_string_to_class[element_type]
                if not element_class in TESTED_ARRAY_CLASSES:
                    raise NotImplementedError(
                        f'Untested array element type "{element_type}"'
                    )
                for element in dictionary["elements"]:
                    elements.append(
                        element_class.from_dict(element, include_header=False)
                    )
            else:
                raise NotImplementedError(
                    f'Unimplemented array property type!: "{element_type}"'
                )
        return cls(unknown, element_type, include_type_header, header_data, elements)

    def _unparseR(self, writer, decode_context):
        if self.element_type == "ByteProperty":  # Hacky ByteProperty fix
            length = len(self.elements[0].data)
        else:
            length = len(self.elements)

        writer.write_data(self.unknown.encode())
        writer.write_string(self.element_type)

        writer.write_u32(self.include_type_header)
        # Extra header info
        if self.include_type_header != 0:
            assert self.header_data  # Make sure header data exists
            if self.element_type == "EnumProperty":
                # We can call unparse_separate_header since we know the type
                EnumProperty.upnarse_separate_header(
                    writer, decode_context, self.header_data
                )
            elif self.element_type == "StructProperty":
                # We can call unparse_separate_header since we know the type
                StructProperty.unparse_separate_header(
                    writer,
                    decode_context,
                    magic=self.include_type_header,
                    header_data=self.header_data,
                )
            else:
                raise NotImplementedError(
                    f'Unknown array type with extra data! Type:"{self.element_type}"'
                )

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)
        writer.write_data(b"\x00" * 1)
        byte_count_start = writer.stream.tell()

        writer.write_u32(length)

        if self.element_type == "ByteProperty":  # Hacky ByteProperty fix
            writer.write_data(self.elements[0].data)
        elif self.element_type in property_string_to_class:
            element_class = property_string_to_class[self.element_type]
            if not element_class in TESTED_ARRAY_CLASSES:
                raise NotImplementedError(
                    f'Untested array element type "{self.element_type}"'
                )
            for element in self.elements:
                element.unparse(
                    writer,
                    decode_context,
                    include_header=False,
                    header_data=self.header_data,
                )
        else:
            writer.write_data(base64.b64decode(self.elements[0]["data"].encode()))

        # Fix for unknown data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        byte_count = current_pos - byte_count_start
        writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

    def _unparseT2(self, writer, decode_context):
        if self.element_type == "ByteProperty":  # Hacky ByteProperty fix
            length = len(self.elements[0].data)
        else:
            length = len(self.elements)

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)

        writer.write_data(self.unknown.encode())
        writer.write_string(self.element_type)

        writer.write_data(b"\x00" * 1)
        byte_count_start = writer.stream.tell()
        writer.write_u32(length)

        if self.element_type == "ByteProperty":  # Hacky ByteProperty fix
            writer.write_data(self.elements[0].data)
        elif self.element_type in property_string_to_class:
            element_class = property_string_to_class[self.element_type]
            if not element_class in TESTED_ARRAY_CLASSES:
                raise NotImplementedError(
                    f'Untested array element type "{self.element_type}"'
                )
            if element_class == StructProperty:
                # We can call unparse_separate_header since we know the type
                StructProperty.unparse_separate_header(
                    writer,
                    decode_context,
                    magic=None,
                    header_data=self.header_data,
                )
            for element in self.elements:
                element.unparse(
                    writer,
                    decode_context,
                    include_header=False,
                    header_data=self.header_data,
                )
        else:
            writer.write_data(base64.b64decode(self.elements[0]["data"].encode()))

        # Fix for unknown data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        byte_count = current_pos - byte_count_start
        writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse(self, writer, decode_context):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, decode_context)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, decode_context)
        else:
            raise NotImplementedError("Unknown game version")


class BoolProperty(BaseObject):
    def __init__(self, header, bool_):
        self.header = header
        self.bool_ = bool_

    @classmethod
    def _parseR(cls, reader, decode_context, include_header=True, header_data=None):
        header = (
            CommonHeader.parse(reader, decode_context, optional_guid=False)
            if include_header
            else None
        )
        bool_ = reader.read_u8()
        if decode_context.game_version == GameVersion.Talos2:
            reader.read_u8()
        return cls(header, bool_)

    @classmethod
    def _parseT2(cls, reader, decode_context, include_header=True, header_data=None):
        header = (
            CommonHeader.parse(reader, decode_context, optional_guid=False)
            if include_header
            else None
        )
        bool_ = reader.read_u8()
        if include_header:
            reader.read_u8()  # Random extra byte in Talos 2?
        return cls(header, bool_)

    @classmethod
    def parse(cls, reader, decode_context, include_header=True, header_data=None):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, decode_context, include_header, header_data)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, decode_context, include_header, header_data)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"bool": self.bool_})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        bool_ = dictionary["bool"]
        return cls(header, bool_)

    def _unparseR(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            # Always 0 bytes
            self.header.unparse(writer, decode_context, 0, optional_guid=False)
        writer.write_u8(self.bool_)

    def _unparseT2(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            # Always 0 bytes
            self.header.unparse(writer, decode_context, 0, optional_guid=False)
        writer.write_u8(self.bool_)
        if include_header:
            writer.write_u8(0)  # Random extra byte TODO: Maybe saveq

    def unparse(self, writer, decode_context, include_header=True, header_data=None):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, decode_context, include_header, header_data)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, decode_context, include_header, header_data)
        else:
            raise NotImplementedError("Unknown game version")


class ByteProperty(BaseObject):
    def __init__(self, header, byte):
        self.header = header
        self.byte = byte

    @classmethod
    def _parseR(cls, reader, decode_context, include_header=True):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        # I don't know why, but bytes are always a string (That probably points to an internal constant)
        byte = reader.read_string()
        return cls(header, byte)

    @classmethod
    def _parseT2(cls, reader, decode_context, include_header=True):
        header = (
            CommonHeader.parse(reader, decode_context, optional_guid=False)
            if include_header
            else None
        )
        # Hacky fix for Talos2 byte properties
        path = reader.read_string()
        unknown = reader.read_u8()
        assert unknown == 0
        value = reader.read_string()
        byte = {
            "path": path,
            "value": value,
        }
        return cls(header, byte)

    @classmethod
    def parse(cls, reader, decode_context, include_header=True):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, decode_context, include_header)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, decode_context, include_header)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"byte": self.byte})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        byte = dictionary["byte"]
        return cls(header, byte)

    def _unparseR(self, writer, decode_context, include_header=True):
        # Basically the same as StrProperty
        if include_header:
            # Write the header with a junk byte count to be replaced once the string length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141)
            byte_count_start = writer.stream.tell()

        writer.write_string(self.byte)

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def _unparseT2(self, writer, decode_context, include_header=True):
        # Hacky fix for Talos2 byte properties
        if include_header:
            # Write the header with a junk byte count to be replaced once the string length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141, optional_guid=False)

        writer.write_string(self.byte["path"])
        writer.write_u8(0)
        byte_count_start = writer.stream.tell()
        writer.write_string(self.byte["value"])

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count, optional_guid=False)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse(self, writer, decode_context, include_header=True):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, decode_context, include_header)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, decode_context, include_header)
        else:
            raise NotImplementedError("Unknown game version")


class DoubleProperty(BaseObject):
    def __init__(self, header, double):
        self.header = header
        self.double = double

    @classmethod
    def parse(cls, reader, decode_context, include_header=True):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        double = reader.read_f64()
        return cls(header, double)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"double": self.double})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        double = dictionary["double"]
        return cls(header, double)

    def unparse(self, writer, decode_context, include_header=True):
        if include_header:
            # Always 8 bytes
            self.header.unparse(writer, decode_context, 8)
        writer.write_f64(self.double)


class EnumProperty(BaseObject):
    def __init__(self, unknown, string1, unknown2, string2, enum_type, enum_data):
        self.unknown = unknown
        self.string1 = string1
        self.unknown2 = unknown2
        self.string2 = string2
        self.enum_type = enum_type
        self.enum_data = enum_data

    @classmethod
    def _parseR(cls, reader, decode_context, include_header=True, header_data=None):
        if include_header:
            non_zero_unknown1 = reader.read_data(4).decode(
                encoding="unicode_escape"
            )  # Unknown
            string1 = reader.read_string()
            non_zero_unknown2 = reader.read_data(4).decode(
                encoding="unicode_escape"
            )  # Unknown
            string2 = reader.read_string()
            reader.read_data(4)  # Unknown
            # Enum type then data
            enum_type = reader.read_string()
        if header_data:
            # The only important value is enum_type
            non_zero_unknown1 = None
            string1 = None
            non_zero_unknown2 = None
            string2 = None
            enum_type = header_data["enum_type"]

        if enum_type in property_string_to_class:
            enum_data = property_string_to_class[enum_type].parse(
                reader, decode_context, include_header=include_header
            )
        else:
            raise NotImplementedError(
                f'Unimplemented enum type!: @{reader.stream.tell():#2x} "{enum_type}"'
            )

        return cls(
            non_zero_unknown1, string1, non_zero_unknown2, string2, enum_type, enum_data
        )

    @classmethod
    def _parseT2(cls, reader, include_header=True, header_data=None):
        non_zero_unknown1 = None
        string1 = None
        non_zero_unknown2 = None
        if include_header:
            reader.read_u32()  # Byte count - Ignore as we always recalculate
            non_zero_unknown1 = reader.read_data(4).decode(
                encoding="unicode_escape"
            )  # Unknown
            string1 = reader.read_string()
            non_zero_unknown2 = reader.read_data(1).decode(
                encoding="unicode_escape"
            )  # Unknown
        string2 = reader.read_string()
        return cls(non_zero_unknown1, string1, non_zero_unknown2, string2, None, None)

    @classmethod
    def parse(cls, reader, decode_context, include_header=True, header_data=None):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, decode_context, include_header, header_data)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, include_header, header_data)
        else:
            raise NotImplementedError("Unknown game version")

    def _parse_separate_headerR(reader, decode_context):
        string1 = reader.read_string()
        non_zero_unknown2 = reader.read_data(4).decode(
            encoding="unicode_escape"
        )  # Unknown
        string2 = reader.read_string()
        reader.read_data(4)  # Unknown
        enum_type = reader.read_string()
        reader.read_data(4)  # Unknown
        return {
            "string1": string1,
            "non_zero_unknown2": non_zero_unknown2,
            "string2": string2,
            "enum_type": enum_type,
        }

    def parse_separate_header(reader, decode_context):
        if decode_context.game_version == GameVersion.Reawakened:
            return EnumProperty._parse_separate_headerR(reader, decode_context)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        if self.unknown:
            ret.update({"unknown": self.unknown})
        if self.string1:
            ret.update({"string1": self.string1})
        if self.unknown2:
            ret.update({"unknown2": self.unknown2})
        if self.string2:
            ret.update({"string2": self.string2})
        if self.enum_type:
            ret.update({"enum_type": self.enum_type})
        if self.enum_data:
            ret.update({"enum_data": self.enum_data.to_dict()})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        unknown = dictionary.get("unknown", "")
        string1 = dictionary.get("string1", "")
        unknown2 = dictionary.get("unknown2", "")
        string2 = dictionary.get("string2", "")
        enum_type = dictionary.get("enum_type", None)
        enum_data = None
        if enum_type in property_string_to_class:
            enum_data = property_string_to_class[enum_type].from_dict(
                dictionary["enum_data"], include_header=include_header
            )
        return cls(unknown, string1, unknown2, string2, enum_type, enum_data)

    def _unparseR(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            writer.write_data(self.unknown.encode())
            writer.write_string(self.string1)
            writer.write_data(self.unknown2.encode())
            writer.write_string(self.string2)
            writer.write_data(b"\x00" * 4)
            writer.write_string(self.enum_type)

        if self.enum_type in property_string_to_class:
            self.enum_data.unparse(
                writer, decode_context, include_header=include_header
            )

    def _unparseT2(self, writer, include_header=True, header_data=None):
        if include_header:
            # Calculate bytes dynamically
            byte_count_pos = writer.stream.tell()
            writer.write_u32(0x41414141)

            writer.write_data(self.unknown.encode())
            writer.write_string(self.string1)
            writer.write_data(self.unknown2.encode())

            byte_count_start = writer.stream.tell()
        writer.write_string(self.string2)

        if include_header:
            # Fix for unknown data length
            current_pos = writer.stream.tell()
            writer.stream.seek(byte_count_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            writer.write_u32(byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse(self, writer, decode_context, include_header=True, header_data=None):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, decode_context, include_header, header_data)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, include_header, header_data)
        else:
            raise NotImplementedError("Unknown game version")

    def _unparse_separate_headerR(writer, decode_context, header_data):
        string1 = header_data["string1"]
        non_zero_unknown2 = header_data["non_zero_unknown2"]
        string2 = header_data["string2"]
        enum_type = header_data["enum_type"]

        writer.write_string(string1)
        writer.write_data(non_zero_unknown2.encode())
        writer.write_string(string2)
        writer.write_data(b"\x00" * 4)

        writer.write_string(enum_type)
        writer.write_data(b"\x00" * 4)

    def unparse_separate_header(writer, decode_context, header_data):
        if decode_context.game_version == GameVersion.Reawakened:
            return EnumProperty._unparse_separate_headerR(
                writer, decode_context, header_data
            )
        else:
            raise NotImplementedError("Unknown game version")


class FloatProperty(BaseObject):
    def __init__(self, header, float_):
        self.header = header
        self.float_ = float_

    @classmethod
    def parse(cls, reader, decode_context, include_header=True):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        float_ = reader.read_f32()
        return cls(header, float_)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"float": self.float_})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        float_ = dictionary["float"]
        return cls(header, float_)

    def unparse(self, writer, decode_context, include_header=True):
        if include_header:
            # Always 4 bytes
            self.header.unparse(writer, decode_context, 4)
        writer.write_f32(self.float_)


class IntProperty(BaseObject):
    def __init__(self, header, int_):
        self.header = header
        self.int_ = int_

    @classmethod
    def parse(cls, reader, decode_context, include_header=True, header_data=None):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        int_ = reader.read_s32()
        return cls(header, int_)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"int": self.int_})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        int_ = dictionary["int"]
        return cls(header, int_)

    def unparse(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            # Always 4 bytes
            self.header.unparse(writer, decode_context, 4)
        writer.write_s32(self.int_)


class MapProperty(BaseObject):
    def __init__(
        self,
        unknown,
        key_type,
        include_key_header,
        key_header_data,
        value_type,
        include_value_header,
        value_header_data,
        unknown2,
        map_data,
    ):
        self.unknown = unknown
        self.key_type = key_type
        self.include_key_header = include_key_header
        self.key_header_data = key_header_data
        self.value_type = value_type
        self.include_value_header = include_value_header
        self.value_header_data = value_header_data
        self.unknown2 = unknown2
        self.map_data = map_data

    @classmethod
    def _parseR(cls, reader, decode_context):
        non_zero_unknown = reader.read_data(4).decode(
            encoding="unicode_escape"
        )  # Unknown
        key_type = reader.read_string()
        include_key_header = reader.read_u32()
        key_header_data = None
        if include_key_header != 0:
            if key_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                key_header_data = StructProperty.parse_separate_header(
                    reader, decode_context, magic=include_key_header
                )
            else:
                raise NotImplementedError(
                    f'Unknown key type with extra data! Key Type:"{key_type}"'
                )

        value_type = reader.read_string()
        include_value_header = reader.read_u32()
        value_header_data = None
        if include_value_header != 0:
            if value_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                value_header_data = StructProperty.parse_separate_header(
                    reader, decode_context, magic=include_value_header
                )
            else:
                raise NotImplementedError(
                    f'Unknown value type with extra data! Value Type:"{value_type}"'
                )
        reader.read_u32()  # Byte count - Ignore as we always recalculate
        non_zero_unknown2 = reader.read_data(1).decode(
            encoding="unicode_escape"
        )  # Unknown
        reader.read_data(4)  # Unknown
        count = reader.read_u32()  # Don't save element count as we always recalculate

        map_data = {}
        if (
            key_type in property_string_to_class
            and value_type in property_string_to_class
        ):
            key_class = property_string_to_class[key_type]
            value_class = property_string_to_class[value_type]
            if (
                key_class == IntProperty
                and value_class == StrProperty
                or key_class == StructProperty
                and value_class == StructProperty
            ):
                pass
            else:
                raise NotImplementedError(
                    f'Untested map element types "{key_type}" & "{value_type}"'
                )
            for _ in range(count):
                key = key_class.parse(
                    reader,
                    decode_context,
                    include_header=False,
                    header_data=key_header_data,
                )
                if isinstance(key, StructProperty):
                    # Used in one of the actor properties. intpoint struct
                    # Convert it to a JSON string so it is hashable & python is happy
                    # Souldn't need to be edited anyway
                    key = json.dumps(key.to_dict())
                value = value_class.parse(
                    reader,
                    decode_context,
                    include_header=False,
                    header_data=value_header_data,
                )
                map_data.update({key: value})
        else:
            raise NotImplementedError(
                f'Unimplemented map type(s)!: @{reader.stream.tell():#2x} "{key_type}" || "{value_type}"'
            )

        return cls(
            non_zero_unknown,
            key_type,
            include_key_header,
            key_header_data,
            value_type,
            include_value_header,
            value_header_data,
            non_zero_unknown2,
            map_data,
        )

    @classmethod
    def _parseT2(cls, reader, decode_context):
        reader.read_u32()  # Byte count - Ignore as we always recalculate
        non_zero_unknown = reader.read_data(4).decode(
            encoding="unicode_escape"
        )  # Unknown
        key_type = reader.read_string()
        value_type = reader.read_string()
        non_zero_unknown2 = reader.read_data(1).decode(
            encoding="unicode_escape"
        )  # Unknown
        reader.read_data(4)  # Unknown
        count = reader.read_u32()  # Don't save element count as we always recalculate

        map_data = {}
        if (
            key_type in property_string_to_class
            and value_type in property_string_to_class
        ):
            key_class = property_string_to_class[key_type]
            value_class = property_string_to_class[value_type]
            if key_class == IntProperty and value_class == StrProperty:
                key_header_data = None
                value_header_data = None
            elif key_class == StructProperty and value_class == StructProperty:
                # Hard coding the struct-struct map becasue the older unreal version doesn't save the
                # struct header information in the map header, preventing the struct type of the key
                # from being known from the data in file

                # Keys are known to be an IntPoint
                key_header_data = {"struct_name": "IntPoint"}
                # Value struct_name is based on the Reawakened struct
                value_header_data = {"struct_name": "EditableMaskRegion"}
            else:
                raise NotImplementedError(
                    f'Untested map element types "{key_type}" & "{value_type}"'
                )
            for _ in range(count):
                key = key_class.parse(
                    reader,
                    decode_context,
                    include_header=False,
                    header_data=key_header_data,
                )
                if isinstance(key, StructProperty):
                    # Used in one of the actor properties. intpoint struct
                    # Convert it to a JSON string so it is hashable & python is happy
                    # Souldn't need to be edited anyway
                    key = json.dumps(key.to_dict())
                value = value_class.parse(
                    reader,
                    decode_context,
                    include_header=False,
                    header_data=value_header_data,
                )
                map_data.update({key: value})
        else:
            raise NotImplementedError(
                f'Unimplemented map type(s)!: @{reader.stream.tell():#2x} "{key_type}" || "{value_type}"'
            )

        return cls(
            non_zero_unknown,
            key_type,
            None,
            key_header_data,
            value_type,
            None,
            value_header_data,
            non_zero_unknown2,
            map_data,
        )

    @classmethod
    def parse(cls, reader, decode_context):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, decode_context)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, decode_context)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        ret.update({"unknown": self.unknown})
        ret.update({"key_type": self.key_type})
        if self.include_key_header:
            ret.update({"include_key_header": self.include_key_header})
        if self.key_header_data:
            ret.update({"key_header_data": self.key_header_data})
        ret.update({"value_type": self.value_type})
        if self.include_value_header:
            ret.update({"include_value_header": self.include_value_header})
        if self.value_header_data:
            ret.update({"value_header_data": self.value_header_data})
        ret.update({"unknown2": self.unknown2})
        # Convert the map_data to something hashable by json.dumps
        new_map_data = {}
        for key, value in self.map_data.items():
            new_key = key.int_ if isinstance(key, IntProperty) else key
            new_value = value.string if isinstance(value, StrProperty) else value
            new_value = (
                value.to_dict() if isinstance(value, StructProperty) else new_value
            )
            new_map_data.update({new_key: new_value})
        ret.update({"map_data": new_map_data})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        unknown = dictionary["unknown"]
        key_type = dictionary["key_type"]
        include_key_header = dictionary.get("include_key_header", 0)
        key_header_data = dictionary.get("key_header_data", None)
        value_type = dictionary["value_type"]
        include_value_header = dictionary.get("include_value_header", 0)
        value_header_data = dictionary.get("value_header_data", None)
        unknown2 = dictionary["unknown2"]
        map_data = dictionary["map_data"]
        # Convert the map_data to the -Property types
        new_map_data = {}
        for key, value in map_data.items():
            new_key = IntProperty(None, int(key)) if key_type == "IntProperty" else key
            new_value = (
                StrProperty(None, value) if value_type == "StrProperty" else value
            )
            new_value = (
                StructProperty.from_dict(value)
                if value_type == "StructProperty"
                else new_value
            )
            new_map_data.update({new_key: new_value})
        return cls(
            unknown,
            key_type,
            include_key_header,
            key_header_data,
            value_type,
            include_value_header,
            value_header_data,
            unknown2,
            new_map_data,
        )

    def _unparseR(self, writer, decode_context):
        count = len(self.map_data)

        writer.write_data(self.unknown.encode())
        writer.write_string(self.key_type)
        writer.write_u32(self.include_key_header)
        if self.key_header_data:
            if self.key_type == "StructProperty":
                StructProperty.unparse_separate_header(
                    writer,
                    decode_context,
                    magic=self.include_key_header,
                    header_data=self.key_header_data,
                )
            else:
                raise NotImplementedError(
                    f'Unknown key type with extra data! Key Type:"{self.key_type}"'
                )

        writer.write_string(self.value_type)
        writer.write_u32(self.include_value_header)
        if self.value_header_data:
            if self.value_type == "StructProperty":
                StructProperty.unparse_separate_header(
                    writer,
                    decode_context,
                    magic=self.include_value_header,
                    header_data=self.value_header_data,
                )
            else:
                raise NotImplementedError(
                    f'Unknown value type with extra data! Value Type:"{self.value_type}"'
                )

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)
        writer.write_data(self.unknown2.encode())
        byte_count_start = writer.stream.tell()

        writer.write_data(b"\x00" * 4)  # Unknown

        # Write map count based on element length
        writer.write_u32(count)

        for key, value in self.map_data.items():
            if (
                self.key_type in property_string_to_class
                and self.value_type in property_string_to_class
            ):
                if (
                    self.key_type == "IntProperty"
                    and self.value_type == "StrProperty"
                    or self.key_type == "StructProperty"
                    and self.value_type == "StructProperty"
                ):
                    pass
                else:
                    raise NotImplementedError(
                        f'Untested map element types "{self.key_type}" & "{self.value_type}"'
                    )

                if self.key_type == "StructProperty":
                    # Used in one of the actor properties. intpoint struct
                    # Need to convert it to a struct property to call unparse
                    key = StructProperty.from_dict(json.loads(key))
                key.unparse(
                    writer,
                    decode_context,
                    include_header=False,
                    header_data=self.key_header_data,
                )
                value.unparse(
                    writer,
                    decode_context,
                    include_header=False,
                    header_data=self.value_header_data,
                )

        # Hacky fix for data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        byte_count = current_pos - byte_count_start
        writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

    def _unparseT2(self, writer, decode_context):
        count = len(self.map_data)

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)

        writer.write_data(self.unknown.encode())
        writer.write_string(self.key_type)
        writer.write_string(self.value_type)
        writer.write_data(self.unknown2.encode())
        byte_count_start = writer.stream.tell()
        writer.write_data(b"\x00" * 4)  # Unknown

        # Write map count based on element length
        writer.write_u32(count)
        for key, value in self.map_data.items():
            if (
                self.key_type in property_string_to_class
                and self.value_type in property_string_to_class
            ):
                if (
                    self.key_type == "IntProperty"
                    and self.value_type == "StrProperty"
                    or self.key_type == "StructProperty"
                    and self.value_type == "StructProperty"
                ):
                    pass
                else:
                    raise NotImplementedError(
                        f'Untested map element types "{self.key_type}" & "{self.value_type}"'
                    )

                if self.key_type == "StructProperty":
                    # Used in one of the actor properties. intpoint struct
                    # Need to convert it to a struct property to call unparse
                    key = StructProperty.from_dict(json.loads(key))
                key.unparse(
                    writer,
                    decode_context,
                    include_header=False,
                    header_data=self.key_header_data,
                )
                value.unparse(
                    writer,
                    decode_context,
                    include_header=False,
                    header_data=self.value_header_data,
                )

        # Hacky fix for data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        byte_count = current_pos - byte_count_start
        writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse(self, writer, decode_context):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, decode_context)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, decode_context)
        else:
            raise NotImplementedError("Unknown game version")


class NamedProperty(BaseObject):
    def __init__(self, name, property_type, property_object):
        self.name = name
        self.property_type = property_type
        self.property_object = property_object

    @classmethod
    def parse(cls, reader, decode_context):
        name = reader.read_string()
        if name == "None":
            # 4 bytes after "None" is a 0 length string
            # Maybe introduce a "NoneProperty" to standadise reading/writing this string?
            reader.read_data(4)
            return None

        property_type = reader.read_string()
        if property_type in property_string_to_class:
            property_object = property_string_to_class[property_type].parse(
                reader, decode_context
            )
        else:
            raise NotImplementedError(
                f'Unimplemented named property type!: @{reader.stream.tell():#2x} "{property_type}"'
            )
        return cls(name, property_type, property_object)

    def to_dict(self):
        ret = {}
        ret.update({"__type": self.property_type})
        assert self.name
        if self.property_object:
            ret.update(self.property_object.to_dict())
        return {self.name: ret}

    @classmethod
    def from_dict(cls, name, data):
        property_type = data.pop("__type")
        if property_type in property_string_to_class:
            property_object = property_string_to_class[property_type].from_dict(data)
        elif name == "None":
            pass
        else:
            raise NotImplementedError(
                f'Unimplemented named property type!: "{property_type}"'
            )
        return cls(name, property_type, property_object)

    def unparse(self, writer, decode_context):
        writer.write_string(self.name)
        writer.write_string(self.property_type)
        if self.property_type in property_string_to_class:
            self.property_object.unparse(writer, decode_context)
        else:
            raise NotImplementedError(
                f'Unimplemented named property type!: "{self.property_type}"'
            )


class ObjectProperty(BaseObject):
    def __init__(self, header, object_):
        self.header = header
        self.object_ = object_

    @classmethod
    def parse(cls, reader, decode_context, include_header=True, header_data=None):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        obj = ScriptObject.parse(reader, decode_context)
        return cls(header, obj)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update(self.object_.to_dict())
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary.pop("header"))
        object_ = ScriptObject.from_dict(dictionary)
        return cls(header, object_)

    def unparse(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            # Write the header with a junk byte count to be replaced once the object length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141)
            byte_count_start = writer.stream.tell()
        self.object_.unparse(writer, decode_context)

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)


class UserContentTypes(IntEnum):
    Unknown = -1
    TargetActor = 0x03
    CachedScriptPath = 0x04
    CachedDatabasePath = 0x06
    AssetPath = 0x07
    ScriptPath = 0x08
    DatabasePath = 0x09
    CachedAssetPath = 0x0B


class ScriptObject(BaseObject):
    def __init__(
        self,
        special,
        user_content_type,
        user_content_args,
        named_properties,
    ):
        self.special = special
        if special == 0:
            # Object ends
            return
        self.user_content_type = user_content_type
        if user_content_type == UserContentTypes.ScriptPath:
            self.script_path = user_content_args["script_path"]
        elif user_content_type == UserContentTypes.AssetPath:
            self.asset_path = user_content_args["asset_path"]
        elif user_content_type == UserContentTypes.CachedScriptPath:
            self.cache_index = user_content_args["cache_index"]
        elif user_content_type == UserContentTypes.TargetActor:
            self.target_actor_index = user_content_args["target_actor_index"]
        elif user_content_type == UserContentTypes.DatabasePath:
            self.database_path = user_content_args["database_path"]
            self.database_index = user_content_args["database_index"]
        elif user_content_type == UserContentTypes.CachedDatabasePath:
            self.database_cache_index = user_content_args["database_cache_index"]
            self.database_index = user_content_args["database_index"]
        elif user_content_type == UserContentTypes.CachedAssetPath:
            self.asset_index = user_content_args["asset_index"]
        else:
            raise NotImplementedError(
                f"Unimplemented user content type: {user_content_type.name}"
            )
        self.named_properties = named_properties

    def get_property(self, name):
        """Get the property whose name matches the argument"""
        for prop in self.named_properties:
            if prop.name == name:
                return prop

    @classmethod
    def parse(cls, reader, decode_context):
        special = reader.read_u8()
        if special == 0:
            # Object ends
            return cls(special, None, None, None)
        if special == 0x02:
            user_content_type = UserContentTypes(reader.read_u8())
        else:
            user_content_type = UserContentTypes(special)

        user_content_args = {}
        if user_content_type == UserContentTypes.ScriptPath:
            script_path = reader.read_string()
            decode_context.script_path_cache.append(script_path)
            user_content_args.update({"script_path": script_path})
        elif user_content_type == UserContentTypes.AssetPath:
            asset_path = reader.read_string()
            decode_context.asset_path_cache.append(asset_path)
            user_content_args.update({"asset_path": asset_path})
        elif user_content_type == UserContentTypes.CachedScriptPath:
            cache_index = reader.read_u32()
            user_content_args.update({"cache_index": cache_index})
        elif user_content_type == UserContentTypes.TargetActor:
            target_actor_index = reader.read_u32()
            user_content_args.update({"target_actor_index": target_actor_index})
        elif user_content_type == UserContentTypes.DatabasePath:
            database_path = reader.read_string()
            database_index = reader.read_s32()
            decode_context.database_path_cache.append(database_path)
            user_content_args.update({"database_path": database_path})
            user_content_args.update({"database_index": database_index})
        elif user_content_type == UserContentTypes.CachedDatabasePath:
            database_cache_index = reader.read_s32()
            database_index = reader.read_s32()
            user_content_args.update({"database_cache_index": database_cache_index})
            user_content_args.update({"database_index": database_index})
        elif user_content_type == UserContentTypes.CachedAssetPath:
            asset_index = reader.read_s32()
            user_content_args.update({"asset_index": asset_index})
        else:
            raise NotImplementedError(
                f"Unimplemented user content type: {user_content_type.name}"
            )

        # Extra null byte padding when `special` == 0x2 only exists in Reawakened
        # Otherwise it is the least significant byte from the length of a peroperty name for the
        # CustomEpisode/CustomLevel script, which can never be a multiple of 256 (false positive)
        # Use the first instance of this as a way to determine which game the file is from
        if decode_context.game_version == GameVersion.Unknown and special == 0x2:
            decode_context.game_version = (
                GameVersion.Reawakened
                if reader.peek(1) == b"\x00"
                else GameVersion.Talos2
            )

        # Extra padding sometimes, noticed it's only the case when `special` == 0x2
        if decode_context.game_version == GameVersion.Reawakened and special == 0x2:
            reader.read_data(1)

        # Guessing this determines if there are named properties
        named_properties = []
        if special == 0x02:
            while True:
                prop = NamedProperty.parse(reader, decode_context)
                if prop == None:
                    break
                named_properties.append(prop)
        elif special not in [0x0B, 0x09, 0x08, 0x07, 0x06, 0x04, 0x03]:
            raise NotImplementedError(
                f"Unknown if special value has named properties: {special:#2x}"
            )

        return cls(
            special,
            user_content_type,
            user_content_args,
            named_properties,
        )

    def to_dict(self):
        ret = {}
        ret.update({"special": self.special})
        if self.special == 0:
            # Object ends
            return ret
        ret.update({"user_content_type": self.user_content_type})
        if hasattr(self, "script_path"):
            ret.update({"script_path": self.script_path})
        if hasattr(self, "asset_path"):
            ret.update({"asset_path": self.asset_path})
        if hasattr(self, "cache_index"):
            ret.update({"cache_index": self.cache_index})
        if hasattr(self, "target_actor_index"):
            ret.update({"target_actor_index": self.target_actor_index})
        if hasattr(self, "database_path"):
            ret.update({"database_path": self.database_path})
        if hasattr(self, "database_cache_index"):
            ret.update({"database_cache_index": self.database_cache_index})
        if hasattr(self, "database_index"):
            ret.update({"database_index": self.database_index})
        if hasattr(self, "asset_index"):
            ret.update({"asset_index": self.asset_index})
        if self.named_properties:
            named_properties = {}
            for prop in self.named_properties:
                named_properties.update(prop.to_dict())
            ret.update({"named_properties": named_properties})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        special = dictionary["special"]
        if special == 0:
            # Object ends
            return cls(special, None, None, None)
        user_content_type = UserContentTypes(dictionary["user_content_type"])
        user_content_args = {}
        if user_content_type == UserContentTypes.ScriptPath:
            user_content_args.update({"script_path": dictionary["script_path"]})
        elif user_content_type == UserContentTypes.AssetPath:
            user_content_args.update({"asset_path": dictionary["asset_path"]})
        elif user_content_type == UserContentTypes.CachedScriptPath:
            user_content_args.update({"cache_index": dictionary["cache_index"]})
        elif user_content_type == UserContentTypes.TargetActor:
            user_content_args.update(
                {"target_actor_index": dictionary["target_actor_index"]}
            )
        elif user_content_type == UserContentTypes.DatabasePath:
            user_content_args.update({"database_path": dictionary["database_path"]})
            user_content_args.update({"database_index": dictionary["database_index"]})
        elif user_content_type == UserContentTypes.CachedDatabasePath:
            user_content_args.update(
                {"database_cache_index": dictionary["database_cache_index"]}
            )
            user_content_args.update({"database_index": dictionary["database_index"]})
        elif user_content_type == UserContentTypes.CachedAssetPath:
            user_content_args.update({"asset_index": dictionary["asset_index"]})
        else:
            raise NotImplementedError(
                f"Unimplemented user content type: {user_content_type.name}"
            )

        named_properties = []
        properties = dictionary.get("named_properties", {})
        for name, data in properties.items():
            named_properties.append(NamedProperty.from_dict(name, data))

        return cls(
            special,
            user_content_type,
            user_content_args,
            named_properties,
        )

    def unparse(self, writer, decode_context):
        writer.write_u8(self.special)
        if self.special == 0:
            # Object ends
            return

        if self.special == 0x2:
            writer.write_u8(self.user_content_type)

        if self.user_content_type == UserContentTypes.ScriptPath:
            writer.write_string(self.script_path)
        elif self.user_content_type == UserContentTypes.AssetPath:
            writer.write_string(self.asset_path)
        elif self.user_content_type == UserContentTypes.CachedScriptPath:
            writer.write_u32(self.cache_index)
        elif self.user_content_type == UserContentTypes.TargetActor:
            writer.write_u32(self.target_actor_index)
        elif self.user_content_type == UserContentTypes.DatabasePath:
            writer.write_string(self.database_path)
            writer.write_u32(self.database_index)
        elif self.user_content_type == UserContentTypes.CachedDatabasePath:
            writer.write_u32(self.database_cache_index)
            writer.write_u32(self.database_index)
        elif self.user_content_type == UserContentTypes.CachedAssetPath:
            writer.write_u32(self.asset_index)
        else:
            raise NotImplementedError(
                f"Unimplemented user content type: {self.user_content_type.name}"
            )

        # Extra null byte padding when `special` == 0x2 only exists in Reawakened
        # Extra padding sometimes, noticed it's only the case when `special` == 0x2
        if (
            decode_context.game_version == GameVersion.Reawakened
            and self.special == 0x02
        ):
            writer.write_data(b"\x00")

        if self.special == 0x02:
            for prop in self.named_properties:
                prop.unparse(writer, decode_context)
            # Write the `None` property
            writer.write_string("None")
            writer.write_data(b"\x00" * 4)


class SoftObjectProperty(BaseObject):
    def __init__(
        self,
        header,
        user_content_type,
        user_content_args,
    ):
        self.header = header
        self.user_content_type = user_content_type
        if user_content_type == UserContentTypes.DatabasePath:
            self.database_path = user_content_args["database_path"]
            self.database_index = user_content_args["database_index"]
        elif user_content_type == UserContentTypes.AssetPath:
            self.package_path = user_content_args["package_path"]
            self.asset_name = user_content_args["asset_name"]
            self.subobject = user_content_args["subobject"]
        elif user_content_type == UserContentTypes.CachedDatabasePath:
            self.database_cache_index = user_content_args["database_cache_index"]
            self.database_index = user_content_args["database_index"]
        elif user_content_type == UserContentTypes.CachedAssetPath:
            self.asset_index = user_content_args["asset_index"]
        else:
            raise NotImplementedError(
                f"Unimplemented soft object user_content_type: {user_content_type.name}"
            )

    @classmethod
    def parse(cls, reader, decode_context, include_header=True):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        user_content_type = UserContentTypes(reader.read_u8())

        user_content_args = {}
        if user_content_type == UserContentTypes.DatabasePath:
            database_path = reader.read_string()
            database_index = reader.read_s32()
            decode_context.database_path_cache.append(database_path)
            user_content_args.update({"database_path": database_path})
            user_content_args.update({"database_index": database_index})
        elif user_content_type == UserContentTypes.AssetPath:
            package_path = reader.read_string()
            asset_name = reader.read_string()
            subobject = reader.read_string()
            user_content_args.update({"package_path": package_path})
            user_content_args.update({"asset_name": asset_name})
            user_content_args.update({"subobject": subobject})
            # Guessing it is also cached (like databases)
            decode_context.asset_path_cache.append(package_path)
        elif user_content_type == UserContentTypes.CachedDatabasePath:
            database_cache_index = reader.read_s32()
            database_index = reader.read_s32()
            user_content_args.update({"database_cache_index": database_cache_index})
            user_content_args.update({"database_index": database_index})
        elif user_content_type == UserContentTypes.CachedAssetPath:
            asset_index = reader.read_s32()
            user_content_args.update({"asset_index": asset_index})
        else:
            raise NotImplementedError(
                f"Unimplemented soft object user_content_type: {user_content_type.name}"
            )
        return cls(
            header,
            user_content_type,
            user_content_args,
        )

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"user_content_type": self.user_content_type})
        if hasattr(self, "database_path"):
            ret.update({"database_path": self.database_path})
        if hasattr(self, "database_index"):
            ret.update({"database_index": self.database_index})
        if hasattr(self, "package_path"):
            ret.update({"package_path": self.package_path})
        if hasattr(self, "asset_name"):
            ret.update({"asset_name": self.asset_name})
        if hasattr(self, "subobject"):
            ret.update({"subobject": self.subobject})
        if hasattr(self, "database_cache_index"):
            ret.update({"database_cache_index": self.database_cache_index})
        if hasattr(self, "asset_index"):
            ret.update({"asset_index": self.asset_index})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary.pop("header"))
        user_content_type = UserContentTypes(dictionary["user_content_type"])
        user_content_args = {}
        if user_content_type == UserContentTypes.DatabasePath:
            user_content_args.update({"database_path": dictionary["database_path"]})
            user_content_args.update({"database_index": dictionary["database_index"]})
        elif user_content_type == UserContentTypes.AssetPath:
            user_content_args.update({"package_path": dictionary["package_path"]})
            user_content_args.update({"asset_name": dictionary["asset_name"]})
            user_content_args.update({"subobject": dictionary["subobject"]})
        elif user_content_type == UserContentTypes.CachedDatabasePath:
            user_content_args.update(
                {"database_cache_index": dictionary["database_cache_index"]}
            )
            user_content_args.update({"database_index": dictionary["database_index"]})
        elif user_content_type == UserContentTypes.CachedAssetPath:
            user_content_args.update({"asset_index": dictionary["asset_index"]})
        else:
            raise NotImplementedError(
                f"Unimplemented soft object user_content_type: {user_content_type.name}"
            )
        return cls(
            header,
            user_content_type,
            user_content_args,
        )

    def unparse(self, writer, decode_context, include_header=True):
        if include_header:
            # Write the header with a junk byte count to be replaced once the object length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141)
            byte_count_start = writer.stream.tell()

        writer.write_u8(self.user_content_type)
        if self.user_content_type == UserContentTypes.DatabasePath:
            writer.write_string(self.database_path)
            writer.write_s32(self.database_index)
        elif self.user_content_type == UserContentTypes.AssetPath:
            writer.write_string(self.package_path)
            writer.write_string(self.asset_name)
            writer.write_string(self.subobject)
        elif self.user_content_type == UserContentTypes.CachedDatabasePath:
            writer.write_s32(self.database_cache_index)
            writer.write_s32(self.database_index)
        elif self.user_content_type == UserContentTypes.CachedAssetPath:
            writer.write_s32(self.asset_index)
        else:
            raise NotImplementedError(
                f"Unimplemented soft object user_content_type: {self.user_content_type.name}"
            )

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)


class StrProperty(BaseObject):
    def __init__(self, header, string):
        self.header = header
        self.string = string

    @classmethod
    def parse(cls, reader, decode_context, include_header=True, header_data=None):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        string = reader.read_string()
        return cls(header, string)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"string": self.string})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        string = dictionary["string"]
        return cls(header, string)

    def unparse(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            # Write the header with a junk byte count to be replaced once the string length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141)
            byte_count_start = writer.stream.tell()

        writer.write_string(self.string)

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)


class StructProperty(BaseObject):
    def __init__(
        self,
        magic,
        struct_name,
        unknown,
        path,
        magic_unknown,
        uuid,
        unknown2,
        data,
        header=None,
    ):
        self.magic = magic
        self.struct_name = struct_name
        self.unknown = unknown
        self.path = path
        self.magic_unknown = magic_unknown
        self.uuid = uuid
        self.unknown2 = unknown2
        self.data = data
        self.header = header

    @classmethod
    def _parseR(cls, reader, decode_context, include_header=True, header_data=None):
        if include_header:
            magic = reader.read_u32()
            struct_name = reader.read_string()
            non_zero_unknown = reader.read_data(4).decode(
                encoding="unicode_escape"
            )  # Unknown
            path = reader.read_string()

            magic_unknown = ""
            uuid = ""
            if magic == 2:
                magic_unknown = reader.read_data(4).decode(encoding="unicode_escape")
                uuid = reader.read_string()

            reader.read_data(4)  # Unknown
            reader.read_u32()  # Byte count - Ignore as we always recalculate
            non_zero_unknown2 = reader.read_data(1).decode(
                encoding="unicode_escape"
            )  # Unknown
        else:
            # In arrays/maps where the struct header exists in the array/map header
            # Make sure the header data is passed as a argument
            assert header_data

        if header_data:
            magic = header_data["magic"]
            struct_name = header_data["struct_name"]
            non_zero_unknown = header_data["non_zero_unknown"]
            path = header_data["path"]
            magic_unknown = header_data["magic_unknown"]
            uuid = header_data["uuid"]
            non_zero_unknown2 = None  # Value doesn't matter as it shouldn't get written

        if struct_name == "Vector":
            vector = struct.unpack("<3d", reader.read_data(8 * 3))
            data = list(vector)
        elif struct_name == "Quat":
            quat = struct.unpack("<4d", reader.read_data(8 * 4))
            data = list(quat)
        elif struct_name == "IntPoint":
            intpoint = struct.unpack("<2i", reader.read_data(4 * 2))
            data = list(intpoint)
        elif struct_name == "Rotator":
            rotator = struct.unpack("<3d", reader.read_data(8 * 3))
            data = list(rotator)
        elif struct_name == "LinearColor":
            colour = struct.unpack("<4f", reader.read_data(4 * 4))
            data = list(colour)
        else:  # Custom struct, not part of core Unreal Engine
            if path == "/Script/CoreUObject" and struct_name != "Transform":
                # Transform is special as it is comprised of 1-3 structs
                print(
                    f"Warning! Struct {struct_name} is likely part of core Unreal Engine and has a known format"
                )
            named_properties = []
            while True:
                prop = NamedProperty.parse(reader, decode_context)
                if prop == None:
                    # Hack for None type having no extra bytes
                    reader.stream.seek(-4, os.SEEK_CUR)
                    break
                named_properties.append(prop)
            data = named_properties
        return cls(
            magic,
            struct_name,
            non_zero_unknown,
            path,
            magic_unknown,
            uuid,
            non_zero_unknown2,
            data,
        )

    @classmethod
    def _parseT2(cls, reader, decode_context, include_header=True, header_data=None):
        if include_header:
            header = CommonHeader.parse(reader, decode_context, optional_guid=False)
            struct_name = reader.read_string()
            unknown = base64.b64encode(reader.read_data(0x11)).decode()
        else:
            assert header_data
            header = None
            struct_name = header_data["struct_name"]
            unknown = None

        if struct_name == "Vector":
            vector = struct.unpack("<3d", reader.read_data(8 * 3))
            data = list(vector)
        elif struct_name == "Quat":
            quat = struct.unpack("<4d", reader.read_data(8 * 4))
            data = list(quat)
        elif struct_name == "IntPoint":
            intpoint = struct.unpack("<2i", reader.read_data(4 * 2))
            data = list(intpoint)
        elif struct_name == "Rotator":
            rotator = struct.unpack("<3d", reader.read_data(8 * 3))
            data = list(rotator)
        elif struct_name == "LinearColor":
            colour = struct.unpack("<4f", reader.read_data(4 * 4))
            data = list(colour)
        else:  # Custom struct, not part of core Unreal Engine
            named_properties = []
            while True:
                prop = NamedProperty.parse(reader, decode_context)
                if prop == None:
                    # Hack for None type having no extra bytes
                    reader.stream.seek(-4, os.SEEK_CUR)
                    break
                named_properties.append(prop)
            data = named_properties
        return cls(
            None, struct_name, unknown, None, None, None, None, data, header=header
        )

    @classmethod
    def parse(cls, reader, decode_context, include_header=True, header_data=None):
        if decode_context.game_version == GameVersion.Reawakened:
            return cls._parseR(reader, decode_context, include_header, header_data)
        elif decode_context.game_version == GameVersion.Talos2:
            return cls._parseT2(reader, decode_context, include_header, header_data)
        else:
            raise NotImplementedError("Unknown game version")

    def _parse_separate_headerR(reader, decode_context, magic):
        struct_name = reader.read_string()
        non_zero_unknown = reader.read_data(4).decode(
            encoding="unicode_escape"
        )  # Unknown
        path = reader.read_string()

        magic_unknown = ""
        uuid = ""
        if magic == 2:
            magic_unknown = reader.read_data(4).decode(encoding="unicode_escape")
            uuid = reader.read_string()

        reader.read_data(4)  # Unknown

        return {
            "magic": magic,
            "struct_name": struct_name,
            "non_zero_unknown": non_zero_unknown,
            "path": path,
            "magic_unknown": magic_unknown,
            "uuid": uuid,
        }

    def _parse_separate_headerT2(reader, decode_context):
        property_name = reader.read_string()  # Duplicate of the array property name
        property_type = reader.read_string()  # Duplicate of the array element type??
        byte_count = (
            reader.read_u32()
        )  # Byte count of all struct elements in an array combined
        reader.read_data(
            4
        )  # Likely the strings thing found in common headers that go unused in Talos2
        struct_name = reader.read_string()
        unknown = base64.b64encode(reader.read_data(0x11)).decode()
        return {
            "property_name": property_name,
            "property_type": property_type,
            "byte_count": byte_count,
            "struct_name": struct_name,
            "unknown": unknown,
        }

    def parse_separate_header(reader, decode_context, magic):
        if decode_context.game_version == GameVersion.Reawakened:
            return StructProperty._parse_separate_headerR(reader, decode_context, magic)
        if decode_context.game_version == GameVersion.Talos2:
            return StructProperty._parse_separate_headerT2(reader, decode_context)
        else:
            raise NotImplementedError("Unknown game version")

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        if self.magic:
            ret.update({"magic": self.magic})
        if self.struct_name:
            ret.update({"struct_name": self.struct_name})
        if self.unknown:
            ret.update({"unknown": self.unknown})
        if self.path:
            ret.update({"path": self.path})
        if self.magic_unknown:
            ret.update({"magic_unknown": self.magic_unknown})
        if self.uuid:
            ret.update({"uuid": self.uuid})
        if self.unknown2:
            ret.update({"unknown2": self.unknown2})
        if self.struct_name in [
            "Vector",
            "Quat",
            "IntPoint",
            "Rotator",
            "LinearColor",
        ]:
            ret.update({self.struct_name: self.data})
        else:  # Custom struct, not part of core Unreal Engine
            named_properties = {}
            for prop in self.data:
                named_properties.update(prop.to_dict())
            ret.update({"data": named_properties})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        magic = dictionary.get("magic", None)
        struct_name = dictionary.get("struct_name", "")
        unknown = dictionary.get("unknown", None)
        path = dictionary.get("path", None)
        magic_unknown = dictionary.get("magic_unknown", "")
        uuid = dictionary.get("uuid", "")
        unknown2 = dictionary.get("unknown2", None)
        if struct_name == "Vector":
            data = dictionary["Vector"]
        elif struct_name == "Quat":
            data = dictionary["Quat"]
        elif struct_name == "IntPoint":
            data = dictionary["IntPoint"]
        elif struct_name == "Rotator":
            data = dictionary["Rotator"]
        elif struct_name == "LinearColor":
            data = dictionary["LinearColor"]
        else:  # Custom struct, not part of core Unreal Engine
            named_properties = []
            for name, data in dictionary["data"].items():
                named_properties.append(NamedProperty.from_dict(name, data))
            data = named_properties
        return cls(
            magic,
            struct_name,
            unknown,
            path,
            magic_unknown,
            uuid,
            unknown2,
            data,
            header=header,
        )

    def _unparseR(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            writer.write_u32(self.magic)
            writer.write_string(self.struct_name)
            writer.write_data(self.unknown.encode())
            writer.write_string(self.path)
            if self.magic == 2:
                if self.magic_unknown:
                    writer.write_data(self.magic_unknown.encode())
                else:
                    writer.write_data(b"\x00" * 4)
                writer.write_string(self.uuid)
            writer.write_data(b"\x00" * 4)

            # Calculate struct bytes dynamically
            byte_count_pos = writer.stream.tell()
            writer.write_u32(0x41414141)
            writer.write_data(self.unknown2.encode())
            byte_count_start = writer.stream.tell()

        if self.struct_name == "Vector":
            vector = self.data
            writer.write_data(struct.pack("<3d", vector[0], vector[1], vector[2]))
        elif self.struct_name == "Quat":
            quat = self.data
            writer.write_data(struct.pack("<4d", quat[0], quat[1], quat[2], quat[3]))
        elif self.struct_name == "IntPoint":
            intpoint = self.data
            writer.write_data(struct.pack("<2i", intpoint[0], intpoint[1]))
        elif self.struct_name == "Rotator":
            rotator = self.data
            writer.write_data(struct.pack("<3d", rotator[0], rotator[1], rotator[2]))
        elif self.struct_name == "LinearColor":
            colour = self.data
            writer.write_data(
                struct.pack("<4f", colour[0], colour[1], colour[2], colour[3])
            )
        else:
            for prop in self.data:
                prop.unparse(writer, decode_context)
            # Write the `None` property
            writer.write_string("None")

        # Hacky fix for data length
        if include_header:
            current_pos = writer.stream.tell()
            writer.stream.seek(byte_count_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            writer.write_u32(byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def _unparseT2(self, writer, decode_context, include_header=True, header_data=None):
        if include_header:
            # Write the header with a junk byte count to be replaced once the string length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141, optional_guid=False)
            writer.write_string(self.struct_name)
            writer.write_data(base64.b64decode(self.unknown))
        byte_count_start = writer.stream.tell()

        if self.struct_name == "Vector":
            vector = self.data
            writer.write_data(struct.pack("<3d", vector[0], vector[1], vector[2]))
        elif self.struct_name == "Quat":
            quat = self.data
            writer.write_data(struct.pack("<4d", quat[0], quat[1], quat[2], quat[3]))
        elif self.struct_name == "IntPoint":
            intpoint = self.data
            writer.write_data(struct.pack("<2i", intpoint[0], intpoint[1]))
        elif self.struct_name == "Rotator":
            rotator = self.data
            writer.write_data(struct.pack("<3d", rotator[0], rotator[1], rotator[2]))
        elif self.struct_name == "LinearColor":
            colour = self.data
            writer.write_data(
                struct.pack("<4f", colour[0], colour[1], colour[2], colour[3])
            )
        else:
            for prop in self.data:
                prop.unparse(writer, decode_context)
            # Write the `None` property
            writer.write_string("None")

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count, optional_guid=False)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse(self, writer, decode_context, include_header=True, header_data=None):
        if decode_context.game_version == GameVersion.Reawakened:
            return self._unparseR(writer, decode_context, include_header, header_data)
        elif decode_context.game_version == GameVersion.Talos2:
            return self._unparseT2(writer, decode_context, include_header, header_data)
        else:
            raise NotImplementedError("Unknown game version")

    def _unparse_separate_headerR(writer, decode_context, magic, header_data):
        # Don't write magic as it's consumed by `ArrayProperty` when parsing
        struct_name = header_data["struct_name"]
        non_zero_unknown = header_data["non_zero_unknown"]
        path = header_data["path"]
        magic_unknown = header_data["magic_unknown"]
        uuid = header_data["uuid"]

        writer.write_string(struct_name)
        writer.write_data(non_zero_unknown.encode())
        writer.write_string(path)
        if magic == 2:
            if magic_unknown:
                writer.write_data(magic_unknown.encode())
            else:
                writer.write_data(b"\x00" * 4)
            writer.write_string(uuid)
        writer.write_data(b"\x00" * 4)

    def _unparse_separate_headerT2(writer, decode_context, header_data):
        property_name = header_data["property_name"]
        property_type = header_data["property_type"]
        byte_count = header_data["byte_count"]
        struct_name = header_data["struct_name"]
        unknown = header_data["unknown"]

        writer.write_string(property_name)
        writer.write_string(property_type)
        writer.write_u32(byte_count)
        writer.write_data(b"\x00" * 4)
        writer.write_string(struct_name)

        writer.write_data(base64.b64decode(unknown))

    def unparse_separate_header(writer, decode_context, magic, header_data):
        if decode_context.game_version == GameVersion.Reawakened:
            return StructProperty._unparse_separate_headerR(
                writer, decode_context, magic, header_data
            )
        elif decode_context.game_version == GameVersion.Talos2:
            return StructProperty._unparse_separate_headerT2(
                writer, decode_context, header_data
            )
        else:
            raise NotImplementedError("Unknown game version")


class TextProperty(BaseObject):
    def __init__(self, header, unknown1, unknown2, text):
        self.header = header
        self.unknown1 = unknown1
        self.unknown2 = unknown2
        self.text = text

    @classmethod
    def parse(cls, reader, decode_context, include_header=True):
        header = CommonHeader.parse(reader, decode_context) if include_header else None
        unknown1 = reader.read_u32()  # Reawakened: 0x12 - Talos2: 0x2
        unknown2 = reader.read_u8()
        assert unknown2 == 0xFF
        text_exists = reader.read_u32()
        text = reader.read_string() if text_exists == 0x1 else ""
        if text_exists != 0x1:
            assert text_exists == 0x0
        return cls(header, unknown1, unknown2, text)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header": self.header.to_dict()})
        ret.update({"unknown1": self.unknown1})
        ret.update({"unknown2": self.unknown2})
        ret.update({"text": self.text})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        unknown1 = dictionary["unknown1"]
        unknown2 = dictionary["unknown2"]
        text = dictionary["text"]
        return cls(header, unknown1, unknown2, text)

    def unparse(self, writer, decode_context, include_header=True):
        if include_header:
            # Write the header with a junk byte count to be replaced once the string length is known
            header_pos = writer.stream.tell()
            self.header.unparse(writer, decode_context, 0x41414141)
            byte_count_start = writer.stream.tell()

        writer.write_u32(self.unknown1)
        writer.write_u8(self.unknown2)
        if self.text:
            writer.write_u32(0x1)
            writer.write_string(self.text)
        else:
            writer.write_u32(0x0)

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            byte_count = current_pos - byte_count_start
            self.header.unparse(writer, decode_context, byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)


# Class for level files
class Level:
    def __init__(
        self,
        main_decode_context,
        actor_properties_decode_context,
        level_script,
    ):
        self.main_decode_context = main_decode_context
        self.actor_properties_decode_context = actor_properties_decode_context
        self.level_script = level_script

    def __repr__(self):
        return f"{self.level_script}"

    def _actor_properties_fix(level_script, decode_context):
        scene = level_script.get_property("Scene")
        if scene:
            actor_properties = scene.property_object.object_.get_property(
                "ActorProperties"
            )
            if actor_properties:
                actor_properties = actor_properties.property_object
                actor_prop_bytes = actor_properties.elements[0].data
                scripts = []
                with io.BytesIO(actor_prop_bytes) as buffer:
                    buf_reader = io.BufferedReader(buffer)
                    reader = BinaryReader(buf_reader)
                    # Unknown what the first 8 bytes are. Always 0
                    reader.read_data(8)
                    # Read scripts until there are no more bytes
                    while reader.stream.peek(1):
                        script = ObjectProperty.parse(
                            reader, decode_context=decode_context, include_header=False
                        )
                        scripts.append(script)
                actor_properties.elements = scripts

    @classmethod
    def from_file(cls, level_path):
        main_decode_context = DecodeContext()
        with open(level_path, "rb") as f:
            reader = BinaryReader(f)
            # Unknown what the first 8 bytes are. Always 0
            reader.read_data(8)
            level_script = ScriptObject.parse(
                reader, decode_context=main_decode_context
            )

        # Replace the ActorProperty ArrayOfBytes object with the parsed script objects
        # Actor properties use a separate string cache (likely because Talos parses them after the main script)
        # Do it here so we can save the actor property cached strings separately
        actor_properties_decode_context = DecodeContext()
        actor_properties_decode_context.game_version = main_decode_context.game_version
        cls._actor_properties_fix(level_script, actor_properties_decode_context)

        return cls(
            main_decode_context,
            actor_properties_decode_context,
            level_script,
        )

    @classmethod
    def from_dict(cls, dictionary):
        level = dictionary
        # Get the decode contexts
        main_decode_context = DecodeContext()
        main_decode_context.game_version = level["game_version"]
        main_decode_context.script_path_cache = level["main_script_path_cache"]

        actor_properties_decode_context = DecodeContext()
        actor_properties_decode_context.game_version = level["game_version"]
        actor_properties_decode_context.script_path_cache = level[
            "actor_properties_script_path_cache"
        ]
        actor_properties_decode_context.database_path_cache = level[
            "actor_properties_database_path_cache"
        ]
        actor_properties_decode_context.asset_path_cache = level[
            "actor_properties_asset_path_cache"
        ]

        # Parse the ActorProperty array to an ArrayOfBytes object to correctly read the JSON
        # Save the parsed actor properties so we don't need to re-parse them
        scene = level["level_script"]["named_properties"]["Scene"]
        if "named_properties" in scene:
            actor_properties = scene["named_properties"]["ActorProperties"]
            saved_actor_properties_scripts = []
            for script in actor_properties["elements"]:
                saved_actor_properties_scripts.append(
                    ObjectProperty.from_dict(script, include_header=False)
                )

            actor_prop_bytes = b""
            with io.BytesIO() as buffer:
                buf_writer = io.BufferedWriter(buffer)
                writer = BinaryWriter(buf_writer)
                # Unknown what the first 8 bytes are. Always 0
                writer.write_data(b"\x00" * 8)
                for script in saved_actor_properties_scripts:
                    script.unparse(
                        writer, actor_properties_decode_context, include_header=False
                    )
                writer.stream.flush()
                actor_prop_bytes = buffer.getvalue()
            actor_properties["elements"] = [
                {"data": base64.b64encode(actor_prop_bytes).decode()}
            ]

        level_script = ScriptObject.from_dict(level["level_script"])

        # Restore the parsed actor properties so we don't need to re-parse them
        scene = level_script.get_property("Scene")
        if scene:
            actor_properties = scene.property_object.object_.get_property(
                "ActorProperties"
            )
            if actor_properties:
                actor_properties = actor_properties.property_object
                actor_properties.elements = saved_actor_properties_scripts

        return cls(
            main_decode_context,
            actor_properties_decode_context,
            level_script,
        )

    def to_json(self, json_path):
        level = {
            "game_version": self.main_decode_context.game_version,
            "main_script_path_cache": self.main_decode_context.script_path_cache,
            "actor_properties_script_path_cache": self.actor_properties_decode_context.script_path_cache,
            "actor_properties_database_path_cache": self.actor_properties_decode_context.database_path_cache,
            "actor_properties_asset_path_cache": self.actor_properties_decode_context.asset_path_cache,
            "level_script": self.level_script.to_dict(),
        }
        with open(json_path, "wb") as f:
            f.write(json.dumps(level, indent=2).encode())

    def to_file(self, level_path):
        scene = self.level_script.get_property("Scene")
        if scene:
            actor_properties = scene.property_object.object_.get_property(
                "ActorProperties"
            )
            if actor_properties:
                actor_properties = actor_properties.property_object
                actor_prop_bytes = b""
                with io.BytesIO() as buffer:
                    buf_writer = io.BufferedWriter(buffer)
                    writer = BinaryWriter(buf_writer)
                    # Unknown what the first 8 bytes are. Always 0
                    writer.write_data(b"\x00" * 8)
                    for script in actor_properties.elements:
                        script.unparse(
                            writer,
                            self.actor_properties_decode_context,
                            include_header=False,
                        )
                    writer.stream.flush()
                    actor_prop_bytes = buffer.getvalue()
                # Save a copy in case the program continues to execute & modify data after writing to a file
                saved_actor_properties = actor_properties.elements.copy()
                actor_properties.elements = [ArrayOfBytes(actor_prop_bytes)]

        with open(level_path, "wb") as f:
            writer = BinaryWriter(f)
            # Unknown what the first 8 bytes are. Always 0
            writer.write_data(b"\x00" * 8)
            self.level_script.unparse(writer, self.main_decode_context)
        # Restore the copy in case the program continues to execute & modify data after writing to a file
        scene = self.level_script.get_property("Scene")
        if scene:
            actor_properties = scene.property_object.object_.get_property(
                "ActorProperties"
            )
            if actor_properties:
                actor_properties = actor_properties.property_object
                actor_properties.elements = saved_actor_properties


# Class for episode files
class Episode:
    def __init__(self, decode_context, episode_script):
        self.decode_context = decode_context
        self.episode_script = episode_script

    def __repr__(self):
        return f"{self.episode_script}"

    @classmethod
    def from_file(cls, episode_path):
        decode_context = DecodeContext()
        with open(episode_path, "rb") as f:
            reader = BinaryReader(f)
            # Unknown what the first 8 bytes are. Always 0
            reader.read_data(8)
            episode_script = ScriptObject.parse(reader, decode_context=decode_context)
        return cls(decode_context, episode_script)

    @classmethod
    def from_dict(cls, dictionary):
        decode_context = DecodeContext()
        episode = dictionary
        decode_context.game_version = episode["game_version"]
        decode_context.script_path_cache = episode["script_path_cache"]
        episode_script = ScriptObject.from_dict(episode["episode_script"])
        return cls(decode_context, episode_script)

    def to_json(self, json_path):
        episode = {
            "game_version": self.decode_context.game_version,
            "script_path_cache": self.decode_context.script_path_cache,
            "episode_script": self.episode_script.to_dict(),
        }
        with open(json_path, "wb") as f:
            f.write(json.dumps(episode, indent=2).encode())

    def to_file(self, episode_path):
        with open(episode_path, "wb") as f:
            writer = BinaryWriter(f)
            # Unknown what the first 8 bytes are. Always 0
            writer.write_data(b"\x00" * 8)
            self.episode_script.unparse(writer, self.decode_context)


# List of classes that I have tested with array
# New classes may have issues with headers
TESTED_ARRAY_CLASSES = [
    BoolProperty,
    EnumProperty,
    IntProperty,
    ObjectProperty,
    StructProperty,
    StrProperty,
]

property_string_to_class = {
    "ArrayProperty": ArrayProperty,
    "BoolProperty": BoolProperty,
    "ByteProperty": ByteProperty,
    "EnumProperty": EnumProperty,
    "FloatProperty": FloatProperty,
    "DoubleProperty": DoubleProperty,
    "IntProperty": IntProperty,
    "MapProperty": MapProperty,
    "ObjectProperty": ObjectProperty,
    "SoftObjectProperty": SoftObjectProperty,
    "StrProperty": StrProperty,
    "NameProperty": StrProperty,  # Acts like a string. Only seen in sign level targets (which doesn't use the user given name)
    "StructProperty": StructProperty,
    "TextProperty": TextProperty,
}

file_classes = {
    "episode": Episode,
    "level": Level,
}


def main():
    parser = argparse.ArgumentParser(
        description="A tool for converting The Talos Principle: Reawakened `.level` & `.episode` files used in custom campaigns to and from JSON for easier editing. Lets you dump a file to .json for manual editing, or create a .level/.episode from .json. Will save a backup when trying to overwrite a file",
    )
    parser.add_argument(
        "input_file",
        help="/path/to/input. File extension determins conversion type - `.episode/.level` -> `.json` | `.json` -> `.level`",
    )
    parser.add_argument("-o", "--output", help="/path/to/output")
    parser.add_argument(
        "-e",
        "--episode",
        action="store_true",
        help="If set, will use the `.episode` extenstion for output file",
    )

    # .level & .episode files are largely handled by Unreal Engine, with each script/object having a `Serialize` function.
    # This results in a file format similar to unreal games saves (GVAS). Its possible those tool can read/edit .episode & .level files
    # However, "Puzzle Editor author did do a lot of customization as to how the custom levels are serialized" (https://discord.com/channels/464411560563965953/1315739667202834484/1359821476093624383)

    args = parser.parse_args()
    input_path = args.input_file
    output_path = args.output
    use_episode_extension = args.episode

    # If a directory is targeted, default to converting the .episode file inside
    if os.path.isdir(input_path):
        input_path = os.path.join(input_path, ".episode")

    # Set output path if it doesn't exists
    if not output_path:
        root, extension = input_path.rsplit(".", 1)
        if extension in ["level", "episode"]:
            # Episodes don't have a file name, only an extension (.episode)
            output_path = root + ".json"
        elif use_episode_extension:
            output_path = root + ".episode"
        else:
            output_path = root + ".level"

    convert_type = "from_json" if input_path.endswith(".json") else "to_json"

    # If reading from writing to JSON, verify the input file exists
    if convert_type == "to_json":
        if not os.path.exists(input_path):
            print(f'Input file doesn\'t exist: "{input_path}"')
            sys.exit(1)
        if not os.path.isfile(input_path):
            print(f'Not a file: "{input_path}"')
            sys.exit(1)

    # If reading from JSON, verify the JSON file exists
    if convert_type == "from_json":
        if not os.path.exists(input_path):
            print(f'JSON file doesn\'t exist: "{input_path}"')
            sys.exit(1)
        if not os.path.isfile(input_path):
            print(f'Not a JSON file: "{input_path}"')
            sys.exit(1)

        # If writing to .episode/.level, save a backup. Some date/time format as talos logs
        if os.path.exists(output_path):
            time_string = datetime.now(datetime.now().astimezone().tzinfo).strftime(
                "%Y.%m.%d-%H.%M.%S"
            )
            backup_path = output_path + "." + time_string + ".bak"
            print(f'Saving backup to: "{backup_path}"')
            shutil.copy(output_path, backup_path)

    print(f"Input: {input_path}")
    print(f"Output: {output_path}")

    if convert_type == "from_json":
        # Load the json to determine the output file type
        with open(input_path, "rb") as f:
            file_data = json.loads(f.read())

        if "level_script" in file_data:
            file = Level.from_dict(file_data)
        elif "episode_script" in file_data:
            file = Episode.from_dict(file_data)
        else:
            print("Unknown JSON file")

        file.to_file(output_path)
    elif convert_type == "to_json":
        # If converting to json (output is .json), take the input_path extension
        _, file_type = input_path.rsplit(".", 1)
        file_class = file_classes[file_type]

        file = file_class.from_file(input_path)
        file.to_json(output_path)


if __name__ == "__main__":
    main()
