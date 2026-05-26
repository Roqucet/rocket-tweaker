#!/usr/bin/env python3

"""
Author: Rocket (Discord: @roqucet)
Created: 2026-01-27
Version: v0.1.1
Description: Gives more freedom for editing TTP:R .level/.episode files.
    Lets you dump a file to .json for manual editing, or create a .level/.episode from .json.
    Will save a backup when trying to overwrite a file
"""

import sys
import argparse
import os
import struct
import json

import shutil
from datetime import datetime
import base64
import io

from enum import auto, Enum, IntEnum

FORCE_LENGTHS = False 
CACHED_STRINGS = list()
SOFT_OBJECT_CACHED_STRINGS = list()

class BinaryReader(object):
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
        return data.decode(encoding=encoding).rstrip('\x00')

class BinaryWriter(object):
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
            string_length += 1      # + 1 for the null byte (but only if there are bytes)
        if not data.isascii():
            # -2 for removed utf-16 marker & +2 for extra null bytes cancel out
            string_length >>= 1
            string_length *= -1
            # Remove utf-16 marker (b'\xff\xfe') as Talos doesn't use it
            bytes_data = bytes_data[2:]
        bytes_data = bytes_data.ljust(string_length if string_length >= 0 else string_length * -2, b'\x00')
        self.write_s32(string_length)
        self.stream.write(bytes_data)

class CommonHeader(object):
    def parse(reader, optional_guid=True):
        strings = list()
        while True:
            string_exists = reader.read_u32()
            if string_exists == 0:
                break
            strings.append(reader.read_string())

        bytes_to_read = reader.read_u32()
        if optional_guid:
            has_guid = reader.read_u8()
            # TODO: find a case where this is true
            # Should fail a level -> JSON -> level test as it isn't unparsed
            if has_guid != 0:
                guid = reader.read_data(16)

        ret = dict()
        if strings:
            ret.update({"strings": strings,})
        ret.update({"bytes": bytes_to_read,})

        return ret

    def unparse(writer, header, replace_byte_count=-1, optional_guid=True):
        if "strings" in header:
            for string in header["strings"]:
                writer.write_u32(1)
                writer.write_string(string)
        writer.write_data(b'\x00' * 4)
        if replace_byte_count != -1:
            writer.write_u32(replace_byte_count)
        else:
            writer.write_u32(header["bytes"])

        if optional_guid:
            writer.write_data(b'\x00' * 1)

class ObjectProperty(object):
    def parse(reader, include_header=True, header_data=None):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        obj = ScriptObject.parse(reader)
        ret.update({"object": obj,})
        return ret

    def unparse(writer, data, include_header=True, header_data=None):
        if include_header:
            header_pos = writer.stream.tell()
            CommonHeader.unparse(writer, data["header"])
            byte_count_start = writer.stream.tell()
            obj = data["object"]
        else:
            obj = data
        ScriptObject.unparse(writer, obj)

        if include_header:
            header = data["header"]
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if FORCE_LENGTHS:
                CommonHeader.unparse(writer, header)
            else:
                byte_count = current_pos - byte_count_start
                CommonHeader.unparse(writer, header, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class SoftObjectUserContentTypes(IntEnum):
    Unknown         = -1
    CachedDatabase  = 0x06
    DirectPath      = 0x07
    Database        = 0x09

class SoftObjectProperty(object):
    def parse(reader, include_header=True):
        global SOFT_OBJECT_CACHED_STRINGS
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        
        obj = dict()
        user_content_type = SoftObjectUserContentTypes(reader.read_u8())
        obj.update({"user_content_type": user_content_type,})
        if user_content_type == SoftObjectUserContentTypes.Database:
            database_path = reader.read_string()
            asset_index = reader.read_s32()
            SOFT_OBJECT_CACHED_STRINGS.append(database_path)
            obj.update({
                    "database_path": database_path,
                    "asset_index": asset_index,
                })
        elif user_content_type == SoftObjectUserContentTypes.DirectPath:
            # Unsure if it is cached
            package_path = reader.read_string()
            asset_name = reader.read_string()
            subobject = reader.read_string()
            obj.update({
                    "package_path": package_path,
                    "asset_name": asset_name,
                    "subobject": subobject,
                })
        elif user_content_type == SoftObjectUserContentTypes.CachedDatabase:
            database_cache_index = reader.read_s32()
            asset_index = reader.read_s32()
            obj.update({
                    "database_cache_index": database_cache_index,
                    "asset_index": asset_index,
                })
        else:
            print(f"Unimplemented soft object user_content_type: {user_content_type}")

        ret.update({"soft_object": obj,})
        return ret

    def unparse(writer, data, include_header=True):
        if include_header:
            header_pos = writer.stream.tell()
            CommonHeader.unparse(writer, data["header"])
            byte_count_start = writer.stream.tell()

        obj = data["soft_object"]

        user_content_type = SoftObjectUserContentTypes(obj["user_content_type"])
        writer.write_u8(user_content_type)
        if user_content_type == SoftObjectUserContentTypes.Database:
            database_path = obj["database_path"]
            asset_index = obj["asset_index"]
            writer.write_string(database_path)
            writer.write_s32(asset_index)
        elif user_content_type == SoftObjectUserContentTypes.DirectPath:
            package_path = obj["package_path"]
            asset_name = obj["asset_name"]
            subobject = obj["subobject"]
            writer.write_string(package_path)
            writer.write_string(asset_name)
            writer.write_string(subobject)
        elif user_content_type == SoftObjectUserContentTypes.CachedDatabase:
            database_cache_index = obj["database_cache_index"]
            asset_index = obj["asset_index"]
            writer.write_s32(database_cache_index)
            writer.write_s32(asset_index)

        if include_header:
            header = data["header"]
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if FORCE_LENGTHS:
                CommonHeader.unparse(writer, header)
            else:
                byte_count = current_pos - byte_count_start
                CommonHeader.unparse(writer, header, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class ArrayProperty(object):
    def parse(reader):
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
        element_type = reader.read_string()
        include_type_header = reader.read_u32()
        header_data = None
        if include_type_header != 0:
            if element_type == "EnumProperty":
                # We can call parse_separate_header since we know the type
                header_data = EnumProperty.parse_separate_header(reader) 
            elif element_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                header_data = StructProperty.parse_separate_header(reader, magic=include_type_header) 
            else:
                print(f"Warning! Unknown array type with extra data! Type:\"{element_type}\". Probable crash")

        bytes_to_read = reader.read_u32()
        reader.read_data(1)     # Unknown
        length = reader.read_u32()

        elements = list()
        if element_type == "ByteProperty":      # Hacky ByteProperty fix
            # Use the fallback, which works & is expected by NamedProperty to deserialize ActorProperties
            # -4 because bytes_to_read is the number of bytes from before the length field
            data = base64.b64encode(reader.read_data(bytes_to_read - 4))
            elements.append({"data": data.decode()})
        elif element_type in property_string_to_class.keys():
            element_class = property_string_to_class[element_type]
            if not element_class in TESTED_ARRAY_CLASSES:
                print(f"Warning! Untested array element type \"{element_type}\". Potential for incorrect parsing / crash")
            for _ in range(length):
                # Wish there was a nice way to do this without using a generic "data" value in the returned property dicts
                # Doing it this way removes unnecessary dictionaries from arrays
                value = list(element_class.parse(reader, include_header=False, header_data=header_data).values())
                assert len(value) == 1
                value = value[0]
                elements.append(value)
        else:
            print(f"Unimplemented array property type!: \"{element_type}\"")
            # -4 because bytes_to_read is the number of bytes from before the length field
            data = base64.b64encode(reader.read_data(bytes_to_read - 4))
            elements.append({"data": data.decode()})

        return {
            "non zero unknown": non_zero_unknown,
            "type": element_type,
            "include_type_header": include_type_header,
            "header_data": header_data,
            "bytes": bytes_to_read,
            "length": length,
            "elements": elements,
        }
    
    def unparse(writer, data):
        non_zero_unknown = data["non zero unknown"]
        element_type = data["type"]
        include_type_header = data["include_type_header"]
        header_data = data["header_data"]
        if FORCE_LENGTHS:
            length = data["length"]
        else:
            if element_type == "ByteProperty":
                # Need to base64 decode the byte array to get the correct length
                length = len(base64.b64decode(data["elements"][0]["data"]))
            else:
                length = len(data["elements"])

        elements = data["elements"]

        writer.write_data(non_zero_unknown.encode())
        writer.write_string(element_type)

        writer.write_u32(include_type_header)
        # Extra header info
        if include_type_header != 0:
            assert header_data     # Make sure header data exists
            if element_type == "EnumProperty":
                # We can call parse_separate_header since we know the type
                EnumProperty.unparse_separate_header(writer, header_data)
            elif element_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                StructProperty.unparse_separate_header(writer, magic=include_type_header, header_data=header_data)
            else:
                print(f"Warning! Unknown array type with extra data! Type:\"{element_type}\". Probable crash")

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)
        writer.write_data(b'\x00' * 1)
        byte_count_start = writer.stream.tell()

        writer.write_u32(length)

        for element in elements:
            if element_type == "ByteProperty":      # Hacky ByteProperty fix
                # Use the fallback, which works & is expected by NamedProperty to deserialize ActorProperties
                writer.write_data(base64.b64decode(element["data"].encode()))
            elif element_type in property_string_to_class.keys():
                element_class = property_string_to_class[element_type]
                if not element_class in TESTED_ARRAY_CLASSES:
                    print(f"Warning! Untested array element type \"{element_type}\". Potential for incorrect unparsing / crash")
                element_class.unparse(writer, element, include_header=False, header_data=header_data)
            else:
                writer.write_data(base64.b64decode(element["data"].encode()))

        # Hacky fix for data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        if FORCE_LENGTHS:
            writer.write_u32(data["bytes"])
        else:
            byte_count = current_pos - byte_count_start
            writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

class MapProperty(object):
    def parse(reader):
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
        key_type = reader.read_string()
        include_key_header = reader.read_u32()
        key_header_data = None
        if include_key_header != 0:
            if key_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                key_header_data = StructProperty.parse_separate_header(reader, magic=include_key_header) 
            else:
                print(f"Warning! Unknown key with extra data! Key Type:\"{key_type}\". Probable crash")

        value_type = reader.read_string()
        include_value_header = reader.read_u32()
        value_header_data = None
        if include_value_header != 0:
            if value_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                value_header_data = StructProperty.parse_separate_header(reader, magic=include_value_header) 
            else:
                print(f"Warning! Unknown value with extra data! Value Type:\"{value_type}\". Probable crash")
        bytes_to_read = reader.read_u32()
        non_zero_unknown2 = reader.read_data(1).decode(encoding="unicode_escape")     # Unknown
        reader.read_data(4)     # Unknown
        count = reader.read_u32()

        map_data = dict()
        if key_type in property_string_to_class.keys() and value_type in property_string_to_class.keys():
            key_class = property_string_to_class[key_type]
            value_class = property_string_to_class[value_type]
            if key_class == IntProperty and value_class == StrProperty:
                pass
            elif key_class == StructProperty and value_class == StructProperty:
                pass
            else:
                print(f"Warning! Untested map element types \"{key_type}\" & \"{value_type}\". Potential for incorrect parsing / crash")
            for _ in range(count):
                # Convert to list, then index 0 gives that value in the returned key
                # Wish there was a nice way to do this without using a generic "data" value in the returned property dicts
                # Doing it this way removes unnecessary dictionaries from maps
                key = list(key_class.parse(reader, include_header=False, header_data=key_header_data).values())
                assert len(key) == 1
                key = key[0]
                if isinstance(key, dict):
                    # Convert it to a JSON string so it is hashable & python is happy
                    key = json.dumps(key)
                value = list(value_class.parse(reader, include_header=False, header_data=value_header_data).values())
                assert len(value) == 1
                value = value[0]
                map_data.update({key: value})
        else:
            print(f"Unimplemented map type(s)!: @{reader.stream.tell():#2x} \"{key_type}\" || \"{value_type}\"")
            map_data.update({"data": base64.b64encode(reader.read_data(bytes_to_read - 8)).decode()})

        return {
            "non_zero_unknown": non_zero_unknown,
            "key_type": key_type,
            "include_key_header": include_key_header,
            "key_header_data": key_header_data,
            "value_type": value_type,
            "include_value_header": include_value_header,
            "value_header_data": value_header_data,
            "bytes": bytes_to_read,
            "non_zero_unknown2": non_zero_unknown2,
            "count": count,
            "map data": map_data,
        }

    def unparse(writer, data):
        non_zero_unknown = data["non_zero_unknown"]
        non_zero_unknown2 = data["non_zero_unknown2"]
        key_type = data["key_type"]
        include_key_header = data["include_key_header"]
        key_header_data = data["key_header_data"]
        value_type = data["value_type"]
        include_value_header = data["include_value_header"]
        value_header_data = data["value_header_data"]
        
        if FORCE_LENGTHS:
            count = data["count"]
        else:
            count = len(data["map data"])
        map_data = data["map data"]

        writer.write_data(non_zero_unknown.encode())
        writer.write_string(key_type)
        writer.write_u32(include_key_header)
        if key_header_data:
            if key_type == "StructProperty":
                StructProperty.unparse_separate_header(writer, magic=include_key_header, header_data=key_header_data)
            else:
                print(f"Warning! Unknown key with extra data! Key Type:\"{key_type}\". Probable crash")

        writer.write_string(value_type)
        writer.write_u32(include_value_header)
        if value_header_data:
            if key_type == "StructProperty":
                StructProperty.unparse_separate_header(writer, magic=include_key_header, header_data=value_header_data)
            else:
                print(f"Warning! Unknown key with extra data! Value Type:\"{value_type}\". Probable crash")

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)
        writer.write_data(non_zero_unknown2.encode())
        byte_count_start = writer.stream.tell()

        writer.write_data(b'\x00' * 4)

        # Write map count based on element length
        writer.write_u32(count)

        for key in map_data:
            if key_type in property_string_to_class.keys() and value_type in property_string_to_class.keys():
                key_class = property_string_to_class[key_type]
                value_class = property_string_to_class[value_type]
                if key_class == IntProperty and value_class == StrProperty:
                    pass
                elif key_class == StructProperty and value_class == StructProperty:
                    pass
                else:
                    print(f"Warning! Untested map element types \"{key_type}\" & \"{value_type}\". Potential for incorrect parsing / crash")
                value = map_data[key]
                if isinstance(key, str):
                    # Convert it from a JSON string so the struct unparser works
                    key = json.loads(key)
                key_class.unparse(writer, key, include_header=False, header_data=key_header_data)
                value_class.unparse(writer, value, include_header=False, header_data=value_header_data)
            else:
                # TODO: Untested
                # Sanity check
                assert key == "data"
                writer.write_data(base64.b64decode(map_data[key].encode()))

        # Hacky fix for data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        if FORCE_LENGTHS:
            writer.write_u32(data["bytes"])
        else:
            byte_count = current_pos - byte_count_start
            writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

class StrProperty(object):
    def parse(reader, include_header=True, header_data=None):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        string = reader.read_string()
        ret.update({"string": string,})
        return ret

    def unparse(writer, data, include_header=True, header_data=None):
        if include_header:
            header_pos = writer.stream.tell()
            CommonHeader.unparse(writer, data["header"], replace_byte_count=0x41414141)
            byte_count_start = writer.stream.tell()
            string = data["string"]
        else:
            string = data

        writer.write_string(string)

        # Hacky fix for utf-16 strings
        if include_header:
            header = data["header"]
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if FORCE_LENGTHS:
                CommonHeader.unparse(writer, header)
            else:
                byte_count = current_pos - byte_count_start
                CommonHeader.unparse(writer, header, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class TextProperty(object):
    def parse(reader, include_header=True):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        unknown1 = reader.read_u32()
        assert unknown1 == 0x12
        unknown2 = reader.read_u8()
        assert unknown2 == 0xff
        text_exists = reader.read_u32()
        text = ""
        if text_exists == 0x1:
            text = reader.read_string()
        else:
            assert text_exists == 0x0
        ret.update({
            "unknown1": unknown1,
            "unknown2": unknown2,
            "text": text,
        })
        return ret

    def unparse(writer, data, include_header=True):
        if include_header:
            text = data["text"]

            header_pos = writer.stream.tell()
            CommonHeader.unparse(writer, data["header"], replace_byte_count=0x41414141)
            byte_count_start = writer.stream.tell()
        else:
            text = data
        
        unknown1 = data["unknown1"]
        unknown2 = data["unknown2"]

        writer.write_u32(unknown1)
        writer.write_u8(unknown2)
        if len(text) > 0:
            writer.write_u32(0x1)
            writer.write_string(text)
        else:
            writer.write_u32(0x0)

        # Hacky fix for utf-16 strings
        if include_header:
            header = data["header"]
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if FORCE_LENGTHS:
                CommonHeader.unparse(writer, header)
            else:
                byte_count = current_pos - byte_count_start
                CommonHeader.unparse(writer, header, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class FloatProperty(object):
    def parse(reader, include_header=True):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        float_data = reader.read_f32()
        ret.update({"float": float_data,})
        return ret

    def unparse(writer, data, include_header=True):
        if include_header:
            # Always 4 bytes
            CommonHeader.unparse(writer, data["header"], replace_byte_count=4)
            float_data = data["float"]
        else:
            float_data = data
        writer.write_f32(float_data)

class DoubleProperty(object):
    def parse(reader, include_header=True):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        double_data = reader.read_f64()
        ret.update({"double": double_data,})
        return ret

    def unparse(writer, data, include_header=True):
        if include_header:
            # Always 8 bytes
            CommonHeader.unparse(writer, data["header"], replace_byte_count=8)
            double_data = data["double"]
        else:
            double_data = data
        writer.write_f64(double_data)

class BoolProperty(object):
    def parse(reader, include_header=True, header_data=None):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader, optional_guid=False)
            ret.update({"header": header})
        bool_data = reader.read_u8()
        ret.update({"bool": bool_data,})
        return ret
    
    def unparse(writer, data, include_header=True, header_data=None):
        if include_header:
            # Always 0 bytes
            CommonHeader.unparse(writer, data["header"], replace_byte_count=0, optional_guid=False)
            bool_data = data["bool"]
        else:
            bool_data = data
        writer.write_u8(bool_data)

class IntProperty(object):
    def parse(reader, include_header=True, header_data=None):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        num = reader.read_s32()
        ret.update({"int": num})
        return ret

    def unparse(writer, data, include_header=True, header_data=None):
        if include_header:
            # Always 4 bytes
            CommonHeader.unparse(writer, data["header"], replace_byte_count=4)
            num = data["int"]
        else:
            num = data
        writer.write_s32(num)

class ByteProperty(object):
    def parse(reader, include_header=True):
        ret = dict()
        if include_header:
            header = CommonHeader.parse(reader)
            ret.update({"header": header})
        # I don't know why, but bytes are always a string (That probably points to an internal constant)
        byte_value = reader.read_string()
        ret.update({"byte_value": byte_value,})
        return ret

    def unparse(writer, data, include_header=True):
        byte_value = data["byte_value"]
        if include_header:
            header_pos = writer.stream.tell()
            CommonHeader.unparse(writer, data["header"], replace_byte_count=0x41414141)
            byte_count_start = writer.stream.tell()

        writer.write_string(byte_value)

        # Calculate header bytes dynamically
        if include_header:
            header = data["header"]
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if FORCE_LENGTHS:
                CommonHeader.unparse(writer, header)
            else:
                byte_count = current_pos - byte_count_start
                CommonHeader.unparse(writer, header, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class EnumProperty(object):
    def parse(reader, include_header=True, header_data=None):
        ret = dict()
        if include_header:
            non_zero_unknown1 = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
            string1 = reader.read_string()
            non_zero_unknown2 = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
            string2 = reader.read_string()
            reader.read_data(4) # Unknown

            # Property type then data
            property_type = reader.read_string()
            ret.update({
                "non_zero_unknown1": non_zero_unknown1,
                "string1": string1,
                "non_zero_unknown2": non_zero_unknown2,
                "string2": string2,
                "type": property_type,
            })
        
        if header_data:
            # The only important value is property_type
            property_type = header_data["type"]

        if property_type in property_string_to_class.keys():
            property_data = property_string_to_class[property_type].parse(reader, include_header=include_header)
        else:
            print(f"Unimplemented enum property type!: @{reader.stream.tell():#2x} \"{property_type}\"")
            # If type is unknown, assume it uses the common header
            header = CommonHeader.parse(reader)
            data = reader.read_data(header["bytes"]).decode(encoding="unicode_escape")
            property_data = {
                "header": header,
                "data": data,
            }
        ret.update({"data": property_data,})
        return ret

    def parse_separate_header(reader):
        string1 = reader.read_string()
        non_zero_unknown2 = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
        string2 = reader.read_string()
        reader.read_data(4) # Unknown

        # Property type
        property_type = reader.read_string()

        reader.read_data(4) # Unknown
        return {
            "string1": string1,
            "non_zero_unknown2": non_zero_unknown2,
            "string2": string2,
            "type": property_type,
        }

    def unparse(writer, data, include_header=True, header_data=None):
        if include_header:
            non_zero_unknown1 = data["non_zero_unknown1"]
            string1 = data["string1"]
            non_zero_unknown2 = data["non_zero_unknown2"]
            string2 = data["string2"]
            property_type = data["type"]

            writer.write_data(non_zero_unknown1.encode())
            writer.write_string(string1)
            writer.write_data(non_zero_unknown2.encode())
            writer.write_string(string2)
            writer.write_data(b'\x00' * 4)

            writer.write_string(property_type)

            property_data = data["data"]
        else:
            property_data = data

        if header_data:
            # The only important value is property_type
            property_type = header_data["type"]

        if property_type in property_string_to_class.keys():
            property_string_to_class[property_type].unparse(writer, property_data, include_header=include_header)
        else:
            print(f"Unimplemented enum property type!: \"{property_type}\"")
            # If type is unknown, assume it uses the common header
            CommonHeader.unparse(writer, property_data["header"])
            writer.write_data(property_data["data"].encode())

    def unparse_separate_header(writer, header_data):
        string1 = header_data["string1"]
        non_zero_unknown2 = header_data["non_zero_unknown2"]
        string2 = header_data["string2"]
        property_type = header_data["type"]

        writer.write_string(string1)
        writer.write_data(non_zero_unknown2.encode())
        writer.write_string(string2)
        writer.write_data(b'\x00' * 4)

        writer.write_string(property_type)
        writer.write_data(b'\x00' * 4)

class NamedProperty(object):
    def parse(reader):
        name = reader.read_string()
        if name == "None":
            reader.read_data(4)
            return None

        property_type = reader.read_string()

        if property_type in property_string_to_class.keys():
            property_data = property_string_to_class[property_type].parse(reader)
        else:
            print(f"Unimplemented named property type!: @{reader.stream.tell():#2x} \"{property_type}\"")
            # If type is unknown, assume it uses the common header
            header = CommonHeader.parse(reader)
            data = reader.read_data(header["bytes"]).decode(encoding="unicode_escape")
            property_data = {
                "header": header,
                "data": data,
            }

        # Hacky fix to parse the ActorProperties byte array. Needlessly specific (name is sufficient)
        if name == "ActorProperties" and property_type == "ArrayProperty" and property_data["type"] == "ByteProperty":
            # Replace the byte array base64 data with the parsed script objects
            # Actor properties use a separate string cache (likely because Talos parses them after the main script)

            # TODO: Hacky (& potentially slow) fix for separate string caches
            global CACHED_STRINGS
            global SOFT_OBJECT_CACHED_STRINGS
            saved_cache = CACHED_STRINGS.copy()
            saved_soft_cache = SOFT_OBJECT_CACHED_STRINGS.copy()
            CACHED_STRINGS = list()
            SOFT_OBJECT_CACHED_STRINGS = list()

            actor_prop_bytes = base64.b64decode(property_data["elements"][0]["data"])
            actor_prop = Episode()
            actor_prop.from_in_memory(actor_prop_bytes)
            property_data["elements"][0]["data"] = actor_prop.episode

            CACHED_STRINGS = saved_cache.copy()
            SOFT_OBJECT_CACHED_STRINGS = saved_soft_cache.copy()

        return {
            "name": name,
            "type": property_type,
            "data": property_data,
        }

    def unparse(writer, data):
        name = data["name"]
        property_type = data["type"]
        property_data = data["data"]

        writer.write_string(name)
        writer.write_string(property_type)

        # Hacky fix to parse the ActorProperties byte array. Needlessly specific (name is sufficient)
        if name == "ActorProperties" and property_type == "ArrayProperty" and property_data["type"] == "ByteProperty":
            # Replace the byte array base64 data with the parsed script objects
            # Actor properties use a separate string cache (likely because Talos parses them after the main script)
            
            # TODO: Hacky (& potentially slow) fix for separate string caches
            global CACHED_STRINGS
            global SOFT_OBJECT_CACHED_STRINGS
            saved_cache = CACHED_STRINGS.copy()
            saved_soft_cache = SOFT_OBJECT_CACHED_STRINGS.copy()
            CACHED_STRINGS = list()
            SOFT_OBJECT_CACHED_STRINGS = list()
            
            actor_prop = Episode()
            actor_prop.episode = property_data["elements"][0]["data"]
            actor_prop_bytes = b''
            actor_prop_bytes = actor_prop.to_in_memory()
            property_data["elements"][0]["data"] = base64.b64encode(actor_prop_bytes).decode()

            CACHED_STRINGS = saved_cache.copy()
            SOFT_OBJECT_CACHED_STRINGS = saved_soft_cache.copy()

        if property_type in property_string_to_class.keys():
            property_string_to_class[property_type].unparse(writer, property_data)
        else:
            print(f"Unimplemented named property type!: \"{property_type}\"")
            # If type is unknown, assume it uses the common header
            CommonHeader.unparse(writer, property_data["header"])
            writer.write_data(property_data["data"].encode())

class StructProperty(object):
    def parse(reader, include_header=True, header_data=None):
        ret = dict()
        if include_header:
            magic = reader.read_u32()
            struct_name = reader.read_string()
            non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
            path = reader.read_string()

            uuid = ""
            if magic == 2:
                unknown = reader.read_data(4).decode(encoding="unicode_escape")
                uuid = reader.read_string()

            reader.read_data(4)     # Unknown
            bytes_to_read = reader.read_u32()
            non_zero_unknown2 = reader.read_data(1).decode(encoding="unicode_escape")     # Unknown
            
            ret.update({
                "magic": magic,
                "struct_name": struct_name,
                "non_zero_unknown": non_zero_unknown,
                "path": path,
                "uuid": uuid,
                "bytes": bytes_to_read,
                "non_zero_unknown2": non_zero_unknown2,
            })

        if header_data:
            struct_name = header_data["struct_name"]
            path = header_data["path"]

        if struct_name == "Vector":
            vector = struct.unpack("<3d", reader.read_data(8 * 3))
            data = {"vector": vector}
        elif struct_name == "Quat":
            quat = struct.unpack("<4d", reader.read_data(8 * 4))
            data = {"quat": quat}
        elif struct_name == "IntPoint":
            intpoint = struct.unpack("<2i", reader.read_data(4 * 2))
            data = {"intpoint": intpoint}
        elif struct_name == "Rotator":
            rotator = struct.unpack("<3d", reader.read_data(8 * 3))
            data = {"rotator": rotator}
        elif struct_name == "LinearColor":
            colour = struct.unpack("<4f", reader.read_data(4 * 4))
            data = {"colour": colour}
        else:       # Custom struct, not part of core Unreal Engine
            if path == "/Script/CoreUObject" and struct_name != "Transform":
                # Transform is special as it is comprised of 1-3 structs
                print(f"Warning! Struct {struct_name} is likely part of core Unreal Engine and has a known format")
            named_properties = list()
            while True:
                prop = NamedProperty.parse(reader)
                if prop == None:
                    # Hack for None type having no extra bytes
                    reader.stream.seek(-4, os.SEEK_CUR)
                    break
                named_properties.append(prop)
            data = named_properties

        ret.update({"data": data,})
        return ret

    def parse_separate_header(reader, magic):
        struct_name = reader.read_string()
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
        path = reader.read_string()

        uuid = ""
        if magic == 2:
            unknown = reader.read_data(4).decode(encoding="unicode_escape")
            uuid = reader.read_string()

        reader.read_data(4)     # Unknown
        
        return {
            "struct_name": struct_name,
            "non_zero_unknown": non_zero_unknown,
            "path": path,
            "uuid": uuid,
        }

    def unparse(writer, data, include_header=True, header_data=None):
        if include_header:
            magic = data["magic"]
            struct_name = data["struct_name"]
            non_zero_unknown = data["non_zero_unknown"]
            path = data["path"]
            uuid = data["uuid"]
            non_zero_unknown2 = data["non_zero_unknown2"]
            inner_data = data["data"]

            writer.write_u32(magic)
            writer.write_string(struct_name)
            writer.write_data(non_zero_unknown.encode())
            writer.write_string(path)
            if magic == 2:
                writer.write_data(b'\x00' * 4)
                writer.write_string(uuid)
            writer.write_data(b'\x00' * 4)

            # Calculate struct bytes dynamically
            byte_count_pos = writer.stream.tell()
            writer.write_u32(0x41414141)
            writer.write_data(non_zero_unknown2.encode())
            byte_count_start = writer.stream.tell()
        else:
            inner_data = data

        if header_data:
            struct_name = header_data["struct_name"]
            path = header_data["path"]

        if struct_name == "Vector":
            vector = inner_data["vector"]
            writer.write_data(struct.pack("<3d", vector[0], vector[1], vector[2]))
        elif struct_name == "Quat":
            quat = inner_data["quat"]
            writer.write_data(struct.pack("<4d", quat[0], quat[1], quat[2], quat[3]))
        elif struct_name == "IntPoint":
            intpoint = inner_data["intpoint"]
            writer.write_data(struct.pack("<2i", intpoint[0], intpoint[1]))
        elif struct_name == "Rotator":
            rotator = inner_data["rotator"]
            writer.write_data(struct.pack("<3d", rotator[0], rotator[1], rotator[2]))
        elif struct_name == "LinearColor":
            colour = inner_data["colour"]
            writer.write_data(struct.pack("<4f", colour[0], colour[1], colour[2], colour[3]))
        else:
            named_properties = inner_data
            for prop in named_properties:
                NamedProperty.unparse(writer, prop)
            # Write the `None` property
            writer.write_string("None")

        # Hacky fix for data length
        if include_header:
            current_pos = writer.stream.tell()
            writer.stream.seek(byte_count_pos, os.SEEK_SET)
            if FORCE_LENGTHS:
                writer.write_u32(data["bytes"])
            else:
                byte_count = current_pos - byte_count_start
                writer.write_u32(byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse_separate_header(writer, magic, header_data):
        # Don't write magic as it's consumed by `ArrayProperty` when parsing
        struct_name = header_data["struct_name"]
        non_zero_unknown = header_data["non_zero_unknown"]
        path = header_data["path"]
        uuid = header_data["uuid"]

        writer.write_string(struct_name)
        writer.write_data(non_zero_unknown.encode())
        writer.write_string(path)
        if magic == 2:
            writer.write_data(b'\x00' * 4)
            writer.write_string(uuid)
        writer.write_data(b'\x00' * 4)

class ObjectUserContentTypes(IntEnum):
    Unknown         = -1
    TargetActor     = 0x03
    CachedPath      = 0x04
    AssetPath       = 0x07
    UncachedPath    = 0x08

class ScriptObject(object):
    def parse(reader):
        global CACHED_STRINGS

        ret = dict()
        special = reader.read_u8()
        ret.update({"special": special,})
        if special == 0:
            # Object ends
            return ret
        if special == 0x02:
            user_content_type = ObjectUserContentTypes(reader.read_u8())
        else:
            user_content_type = ObjectUserContentTypes(special)

        ret.update({"user_content_type": user_content_type,})
        if user_content_type == ObjectUserContentTypes.UncachedPath:
            object_path = reader.read_string()
            CACHED_STRINGS.append(object_path)
            ret.update({"object_path": object_path,})
        elif user_content_type == ObjectUserContentTypes.AssetPath:
            # TODO: Unsure if this gets cached
            asset_path = reader.read_string()
            ret.update({"asset_path": asset_path,})
        elif user_content_type == ObjectUserContentTypes.CachedPath:
            cache_index = reader.read_u32()
            ret.update({"cache_index": cache_index,})
        elif user_content_type == ObjectUserContentTypes.TargetActor:
            target_actor_index = reader.read_u32()
            ret.update({"target_actor_index": target_actor_index,})
        else:
            print(f"Unimplemented user content type: {user_content_type:#2x}")

        # Extra padding sometimes, noticed it's the case when `special` == 0x2
        if special == 0x2:
            reader.read_data(1)

        # Guessing this determines if there are named properties
        if special == 0x02:
            named_properties = list()
            while True:
                prop = NamedProperty.parse(reader)
                if prop == None:
                    break
                named_properties.append(prop)
            ret.update({"named_properties": named_properties,})
        elif special == 0x08 or special == 0x07 or special == 0x03 or special == 0x4:
            # No named properties
            pass
        else:
            print(f"Unknown special value: {special:#2x}")

        return ret

    def unparse(writer, data):
        special = data["special"]
        writer.write_u8(special)
        if special == 0:
            # Object ends
            return

        user_content_type = data["user_content_type"]
        if special == 0x02:
            writer.write_u8(user_content_type)

        if user_content_type == ObjectUserContentTypes.UncachedPath:
            object_path = data["object_path"]
            writer.write_string(object_path)
        elif user_content_type == ObjectUserContentTypes.AssetPath:
            asset_path = data["asset_path"]
            writer.write_string(asset_path)
        elif user_content_type == ObjectUserContentTypes.CachedPath:
            cache_index = data["cache_index"]
            writer.write_u32(cache_index)
        elif user_content_type == ObjectUserContentTypes.TargetActor:
            target_actor_index = data["target_actor_index"]
            writer.write_u32(target_actor_index)

        # Extra padding sometimes, noticed it's the case when `special` == 0x2
        if special == 0x02:
            writer.write_data(b'\x00')
        
        if special == 0x02:
            named_properties = data["named_properties"]
            for prop in named_properties:
                NamedProperty.unparse(writer, prop)
            # Write the `None` property
            writer.write_string("None")
            writer.write_data(b'\x00' * 4)

class Episode(object):
    def __init__(self):
        self.episode = dict()

    def from_episode(self, episode_path):
        with open(episode_path, "rb") as f:
            self.reader = BinaryReader(f)

            # Unknown what the first 8 bytes are. Always 0
            self.reader.read_data(8)

            scripts = list()

            # Read scripts until there are no more bytes
            while self.reader.stream.peek(1):
                script = ScriptObject.parse(self.reader)
                scripts.append(script)
            self.episode["episode"] = scripts

    def from_json(self, json_path):
        with open(json_path, "rb") as f:
            self.episode = json.loads(f.read())

    def from_in_memory(self, bytes_data):
        with io.BytesIO(bytes_data) as buffer:
            buf_reader = io.BufferedReader(buffer)
            self.reader = BinaryReader(buf_reader)

            # Unknown what the first 8 bytes are. Always 0
            self.reader.read_data(8)

            scripts = list()
            # Read scripts until there are no more bytes
            while self.reader.stream.peek(1):
                script = ScriptObject.parse(self.reader)
                scripts.append(script)

            self.episode["episode"] = scripts

    def to_json(self, json_path):
        with open(json_path, "wb") as f:
            f.write(json.dumps(self.episode, indent=2).encode())

    def to_episode(self, episode_path):
        with open(episode_path, "wb") as f:
            self.writer = BinaryWriter(f)

            # Unknown what the first 8 bytes are. Always 0
            self.writer.write_data(b'\x00' * 8)

            for script in self.episode["episode"]:
                ScriptObject.unparse(self.writer, script)

    def to_in_memory(self):
        with io.BytesIO() as buffer:
            buf_writer = io.BufferedWriter(buffer)
            self.writer = BinaryWriter(buf_writer)

            # Unknown what the first 8 bytes are. Always 0
            self.writer.write_data(b'\x00' * 8)

            for script in self.episode["episode"]:
                ScriptObject.unparse(self.writer, script)
            
            self.writer.stream.flush()
            return buffer.getvalue()

    def dump(self):
        print(json.dumps(self.episode, indent=2))

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
        "NameProperty": StrProperty,        # Acts like a string. Only seen in sign level targets (which doesn't use the user given name)
        "StructProperty": StructProperty,
        "TextProperty": TextProperty,
    }

def main():
    # Check python version since we make use of ordered dictionaries which are standard in 3.7+
    # Ordered dictionaries are to ensure element order is preseved (which might not matter to unreal)
    if sys.version_info[0] < 3 or sys.version_info[1] < 7:
        raise Exception("Must be using Python 3.7 or newer")

    parser = argparse.ArgumentParser(
                        description="A tool for converting The Talos Principle: Reawakened `.level` & `.episode` files used in custom campaigns to and from JSON for easier editing. Lets you dump a file to .json for manual editing, or create a .level/.episode from .json. Will save a backup when trying to overwrite a file",)
    parser.add_argument("input_file", help="/path/to/input. File extension determins conversion type - `.episode/.level` -> `.json` | `.json` -> `.level`")
    parser.add_argument("-o", "--output", help="/path/to/output")
    parser.add_argument("-e", "--episode", action="store_true", help="If set, will use the `.episode` extenstion for output file")
    parser.add_argument("--force_lengths", action="store_true", help="If set, will use the data lengths found in the JSON. Otherwise, will ignore data lengths can calculate them dynamically")

    # .level & .episode files are largely handled by Unreal Engine, with each script/object having a `Serialize` function.
    # This results in a file format similar to unreal games saves (GVAS). Its possible those tool can read/edit .episode & .level files
    # However, "Puzzle Editor author did do a lot of customization as to how the custom levels are serialized" (https://discord.com/channels/464411560563965953/1315739667202834484/1359821476093624383)
    
    # This tool can read .level files!!

    args = parser.parse_args()
    input_path = args.input_file
    output_path = args.output
    use_episode_extension = args.episode

    global FORCE_LENGTHS
    if args.force_lengths:
        FORCE_LENGTHS = args.force_lengths
        print("Warning: There is no sanity checking on byte lengths & element counts. Make sure to have updated all fields correctly")

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
            print(f"Input file doesn't exist: \"{input_path}\"")
            exit(1)
        if not os.path.isfile(input_path):
            print(f"Not a file: \"{input_path}\"")
            exit(1)

    # If reading from JSON, verify the JSON file exists
    if convert_type == "from_json":
        if not os.path.exists(input_path):
            print(f"JSON file doesn't exist: \"{input_path}\"")
            exit(1)
        if not os.path.isfile(input_path):
            print(f"Not a JSON file: \"{input_path}\"")
            exit(1)
        
        # If writing to .episode/.level, save a backup
        if os.path.exists(output_path):
            time_string = datetime.now().strftime("%Y.%m.%d-%H.%M.%S")
            backup_path = output_path + "." + time_string +  ".bak"
            print(f"Saving backup to: \"{backup_path}\"")
            shutil.copy(output_path, backup_path)

    print(f"Input: {input_path}")
    print(f"Output: {output_path}")

    e = Episode()
    if convert_type == "from_json":
        e.from_json(input_path)
        e.to_episode(output_path)
    elif convert_type == "to_json":
        e.from_episode(input_path)
        e.to_json(output_path)


if __name__ == "__main__":
    main()