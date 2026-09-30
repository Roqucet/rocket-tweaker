#!/usr/bin/env python3

"""
Author: Rocket (Discord: @roqucet)
Created: 2026-01-27
Version: v0.2.0
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
from datetime import datetime

USE_LENGTHS = False 
CACHED_STRINGS = []
SOFT_OBJECT_CACHED_STRINGS = []

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
        return data.decode(encoding=encoding).rstrip('\x00')

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
    def from_level(cls, reader):
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
    def to_level(self, writer):
        """Write an object to a level file byte stream"""
        raise NotImplementedError

class CommonHeader(BaseObject):
    def __init__(self, strings, property_length, guid):
        self.strings = strings
        # self.property_length = property_length
        self.guid = guid

    # Override __bool__ so we return false if there's no strings or guid
    def __bool__(self):
        return bool(self.strings or self.guid)

    @classmethod
    def create_empty(cls):
        return cls([], -1, None)

    @classmethod
    def from_level(cls, reader, optional_guid = True):
        strings = []
        while True:
            string_exists = reader.read_u32()
            if string_exists == 0:
                break
            strings.append(reader.read_string())

        property_length = reader.read_u32()
        guid = None
        if optional_guid:
            has_guid = reader.read_u8()
            # TODO: find a case where this is true
            # Should fail a level -> JSON -> level test as it isn't unparsed
            if has_guid != 0:
                guid = reader.read_data(16)

        return cls(strings, property_length, guid)

    def to_dict(self):
        ret = {}
        if self.strings:
            ret.update({"strings" : self.strings})
        # TODO: Calculate property length dynamically?
        # TODO: Is this needed?? Shouldn't need to be edited as it should be calculated dynamically
        # when converting to a .level
        # ret.update({"property_length" : self.property_length})
        if self.guid:
            ret.update({"guid" : self.guid})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        strings = []
        if "strings" in dictionary:
            strings = dictionary["strings"]
        property_length = dictionary["property_length"]
        guid = None
        if "guid" in dictionary:
            guid = dictionary["guid"]
        return cls(strings, property_length, guid)

    def to_level(self, writer, replace_byte_count = -1, optional_guid = True):
        for string in self.strings:
            writer.write_u32(1)
            writer.write_string(string)
        writer.write_data(b'\x00' * 4)
        if replace_byte_count != -1:
            writer.write_u32(replace_byte_count)
        else:
            writer.write_u32(self.property_length)

        # TODO: find a case where this is true
        # Should fail a level -> JSON -> level test as it isn't unparsed
        if optional_guid:
            writer.write_data(b'\x00' * 1)

class ArrayOfBytes:
    def __init__(self, data):
        self.data = data
    
    def __repr__(self):
        return "<" + self.data + ">"

    def to_dict(self):
        return {"data" : self.data}
    
    @classmethod
    def from_dict(cls, dictionary):
        return cls(dictionary["data"])

class ArrayProperty(BaseObject):
    def __init__(self, unknown, element_type, include_type_header, header_data, byte_count, length, elements):
        self.unknown = unknown
        self.element_type = element_type
        self.include_type_header = include_type_header
        self.header_data = header_data
        # self.byte_count = byte_count
        self.length = length
        self.elements = elements

    @classmethod
    def from_level(cls, reader):
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
        element_type = reader.read_string()
        include_type_header = reader.read_u32()
        header_data = None
        if include_type_header != 0:
            if element_type == "EnumProperty":
                # We can call parse_separate_header since we know the type
                # header_data = EnumProperty.parse_separate_header(reader)
                pass
            elif element_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                header_data = StructProperty.parse_separate_header(reader, magic=include_type_header) 
            else:
                print(f"Warning! Unknown array type with extra data! Type:\"{element_type}\". Probable crash")

        bytes_to_read = reader.read_u32()
        reader.read_data(1)     # Unknown
        length = reader.read_u32()

        elements = []
        if element_type == "ByteProperty":      # Hacky ByteProperty fix        # TODO: Fix after rewrite
            # Use the fallback, which works & is expected by NamedProperty to deserialize ActorProperties
            # -4 because bytes_to_read is the number of bytes from before the length field
            data = base64.b64encode(reader.read_data(bytes_to_read - 4))
            elements.append(ArrayOfBytes(data.decode()))
        elif element_type in property_string_to_class:
            element_class = property_string_to_class[element_type]
            if not element_class in TESTED_ARRAY_CLASSES:
                print(f"Warning! Untested array element type \"{element_type}\". Potential for incorrect parsing / crash")
            for _ in range(length):
                elements.append(element_class.from_level(reader, include_header=False, header_data=header_data))
        else:
            print(f"Unimplemented array property type!: \"{element_type}\"")
            # -4 because bytes_to_read is the number of bytes from before the length field
            data = base64.b64encode(reader.read_data(bytes_to_read - 4))
            elements.append({"data": data.decode()})

        return cls(non_zero_unknown, element_type, include_type_header, header_data, bytes_to_read, length, elements)
    
    def to_dict(self):
        ret = {}
        ret.update({"unknown" : self.unknown})
        ret.update({"element_type" : self.element_type})
        ret.update({"include_type_header" : self.include_type_header})
        if self.header_data:
            ret.update({"header_data" : self.header_data})
        ret.update({"length" : self.length})
        if self.elements:
            elements = []
            for element in self.elements:
                elements.append(element.to_dict())
            ret.update({"elements" : elements})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        unknown = dictionary["unknown"]
        element_type = dictionary["element_type"]
        include_type_header = dictionary["include_type_header"]
        header_data = None
        if "header_data" in dictionary:
            header_data = dictionary["header_data"]
        # byte_count = dictionary["byte_count"]
        byte_count = -1
        length = dictionary["length"]
        elements = []
        if "elements" in dictionary:
            if element_type == "ByteProperty":      # Hacky ByteProperty fix        # TODO: Fix after rewrite
                # Use the fallback, which works & is expected by NamedProperty to deserialize ActorProperties
                # -4 because bytes_to_read is the number of bytes from before the length field
                elements.append(ArrayOfBytes.from_dict(dictionary["elements"][0]))
            elif element_type in property_string_to_class:
                element_class = property_string_to_class[element_type]
                if not element_class in TESTED_ARRAY_CLASSES:
                    print(f"Warning! Untested array element type \"{element_type}\". Potential for incorrect parsing / crash")
                for element in dictionary["elements"]:
                    elements.append(element_class.from_dict(element, include_header=False))
            else:
                print(f"Unimplemented array property type!: \"{element_type}\"")
                # -4 because bytes_to_read is the number of bytes from before the length field
                data = base64.b64decode(dictionary["elements"][0].encode())
                elements.append(data)
        return cls(unknown, element_type, include_type_header, header_data, byte_count, length, elements)

    def to_level(self, writer):
        if USE_LENGTHS:
            length = self.length
        else:
            if self.element_type == "ByteProperty":
                # Need to base64 decode the byte array to get the correct length
                length = len(base64.b64decode(self.elements[0].data))
            else:
                length = len(self.elements)

        writer.write_data(self.unknown.encode())
        writer.write_string(self.element_type)

        writer.write_u32(self.include_type_header)
        # Extra header info
        if self.include_type_header != 0:
            assert self.header_data     # Make sure header data exists
            if self.element_type == "EnumProperty":
                # We can call parse_separate_header since we know the type
                # EnumProperty.unparse_separate_header(writer, self.header_data)
                pass
            elif self.element_type == "StructProperty":
                # We can call parse_separate_header since we know the type
                StructProperty.unparse_separate_header(writer, magic=self.include_type_header, header_data=self.header_data)
            else:
                print(f"Warning! Unknown array type with extra data! Type:\"{self.element_type}\". Probable crash")

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)
        writer.write_data(b'\x00' * 1)
        byte_count_start = writer.stream.tell()

        writer.write_u32(length)

        if self.element_type == "ByteProperty":      # Hacky ByteProperty fix
            # Use the fallback, which works & is expected by NamedProperty to deserialize ActorProperties
            writer.write_data(base64.b64decode(self.elements[0].data))
        elif self.element_type in property_string_to_class:
            for element in self.elements:
                element_class = property_string_to_class[self.element_type]
                if not element_class in TESTED_ARRAY_CLASSES:
                    print(f"Warning! Untested array element type \"{self.element_type}\". Potential for incorrect unparsing / crash")
                element.to_level(writer, include_header=False, header_data=self.header_data)
                # element_class.to_level(writer, element, include_header=False, header_data=self.header_data)
        else:
            writer.write_data(base64.b64decode(self.elements[0]["data"].encode()))

        # Hacky fix for data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        if USE_LENGTHS:
            writer.write_u32(self.bytes)
        else:
            byte_count = current_pos - byte_count_start
            writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

class BoolProperty(BaseObject):
    def __init__(self, header, bool_):
        self.header = header
        self.bool_ = bool_

    @classmethod
    def from_level(cls, reader, include_header = True, header_data = None):
        header = CommonHeader.from_level(reader, optional_guid=False) if include_header else None
        bool_ = reader.read_u8()
        return cls(header, bool_)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header" : self.header.to_dict()})
        ret.update({"bool" : self.bool_})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        bool_ = dictionary["bool"]
        return cls(header, bool_)

    def to_level(self, writer, include_header = True, header_data = None):
        if include_header:
            # Always 0 bytes
            self.header.to_level(writer, replace_byte_count=0, optional_guid=False)
        writer.write_u8(self.bool_)

# class ByteProperty(BaseObject):
#     def from_level(reader, include_header=True):
#         ret = dict()
#         if include_header:
#             header = CommonHeader.from_level(reader)
#             ret.update({"header": header})
#         # I don't know why, but bytes are always a string (That probably points to an internal constant)
#         byte_value = reader.read_string()
#         ret.update({"byte_value": byte_value,})
#         return ret

#     def to_level(self, writer, include_header=True):
#         byte_value = data["byte_value"]
#         if include_header:
#             header_pos = writer.stream.tell()
#             CommonHeader.to_level(writer, data["header"], replace_byte_count=0x41414141)
#             byte_count_start = writer.stream.tell()

#         writer.write_string(byte_value)

#         # Calculate header bytes dynamically
#         if include_header:
#             header = data["header"]
#             current_pos = writer.stream.tell()
#             writer.stream.seek(header_pos, os.SEEK_SET)
#             if USE_LENGTHS:
#                 CommonHeader.to_level(writer, header)
#             else:
#                 byte_count = current_pos - byte_count_start
#                 CommonHeader.to_level(writer, header, replace_byte_count=byte_count)
#             writer.stream.seek(current_pos, os.SEEK_SET)

# class DoubleProperty(BaseObject):
#     def from_level(reader, include_header=True):
#         ret = dict()
#         if include_header:
#             header = CommonHeader.from_level(reader)
#             ret.update({"header": header})
#         double_data = reader.read_f64()
#         ret.update({"double": double_data,})
#         return ret

#     def to_level(self, writer, include_header=True):
#         if include_header:
#             # Always 8 bytes
#             CommonHeader.to_level(writer, data["header"], replace_byte_count=8)
#             double_data = data["double"]
#         else:
#             double_data = data
#         writer.write_f64(double_data)

# class EnumProperty(BaseObject):
#     def from_level(reader, include_header=True, header_data=None):
#         ret = dict()
#         if include_header:
#             non_zero_unknown1 = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
#             string1 = reader.read_string()
#             non_zero_unknown2 = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
#             string2 = reader.read_string()
#             reader.read_data(4) # Unknown

#             # Property type then data
#             property_type = reader.read_string()
#             ret.update({
#                 "non_zero_unknown1": non_zero_unknown1,
#                 "string1": string1,
#                 "non_zero_unknown2": non_zero_unknown2,
#                 "string2": string2,
#                 "type": property_type,
#             })
        
#         if header_data:
#             # The only important value is property_type
#             property_type = header_data["type"]

#         if property_type in property_string_to_class.keys():
#             property_data = property_string_to_class[property_type].from_level(reader, include_header=include_header)
#         else:
#             print(f"Unimplemented enum property type!: @{reader.stream.tell():#2x} \"{property_type}\"")
#             # If type is unknown, assume it uses the common header
#             header = CommonHeader.from_level(reader)
#             data = reader.read_data(header["bytes"]).decode(encoding="unicode_escape")
#             property_data = {
#                 "header": header,
#                 "data": data,
#             }
#         ret.update({"data": property_data,})
#         return ret

#     def parse_separate_header(reader):
#         string1 = reader.read_string()
#         non_zero_unknown2 = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
#         string2 = reader.read_string()
#         reader.read_data(4) # Unknown

#         # Property type
#         property_type = reader.read_string()

#         reader.read_data(4) # Unknown
#         return {
#             "string1": string1,
#             "non_zero_unknown2": non_zero_unknown2,
#             "string2": string2,
#             "type": property_type,
#         }

#     def to_level(self, writer, include_header=True, header_data=None):
#         if include_header:
#             non_zero_unknown1 = data["non_zero_unknown1"]
#             string1 = data["string1"]
#             non_zero_unknown2 = data["non_zero_unknown2"]
#             string2 = data["string2"]
#             property_type = data["type"]

#             writer.write_data(non_zero_unknown1.encode())
#             writer.write_string(string1)
#             writer.write_data(non_zero_unknown2.encode())
#             writer.write_string(string2)
#             writer.write_data(b'\x00' * 4)

#             writer.write_string(property_type)

#             property_data = data["data"]
#         else:
#             property_data = data

#         if header_data:
#             # The only important value is property_type
#             property_type = header_data["type"]

#         if property_type in property_string_to_class.keys():
#             property_string_to_class[property_type].to_level(writer, property_data, include_header=include_header)
#         else:
#             print(f"Unimplemented enum property type!: \"{property_type}\"")
#             # If type is unknown, assume it uses the common header
#             CommonHeader.to_level(writer, property_data["header"])
#             writer.write_data(property_data["data"].encode())

#     def unparse_separate_header(writer, header_data):
#         string1 = header_data["string1"]
#         non_zero_unknown2 = header_data["non_zero_unknown2"]
#         string2 = header_data["string2"]
#         property_type = header_data["type"]

#         writer.write_string(string1)
#         writer.write_data(non_zero_unknown2.encode())
#         writer.write_string(string2)
#         writer.write_data(b'\x00' * 4)

#         writer.write_string(property_type)
#         writer.write_data(b'\x00' * 4)

class FloatProperty(BaseObject):
    def __init__(self, header, float_):
        self.header = header
        self.float_ = float_

    @classmethod
    def from_level(cls, reader, include_header=True):
        header = CommonHeader.from_level(reader) if include_header else None
        float_ = reader.read_f32()
        return cls(header, float_)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header" : self.header.to_dict()})
        ret.update({"float" : self.float_})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        float_ = dictionary["float"]
        return cls(header, float_)

    def to_level(self, writer, include_header=True):
        if include_header:
            # Always 4 bytes
            self.header.to_level(writer, replace_byte_count=4)
        writer.write_f32(self.float_)

class IntProperty(BaseObject):
    def __init__(self, header, int_):
        self.header = header
        self.int_ = int_

    @classmethod
    def from_level(cls, reader, include_header = True, header_data = None):
        header = CommonHeader.from_level(reader) if include_header else None
        int_ = reader.read_s32()
        return cls(header, int_)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header" : self.header.to_dict()})
        ret.update({"int" : self.int_})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        int_ = dictionary["int"]
        return cls(header, int_)

    def to_level(self, writer, include_header = True, header_data = None):
        if include_header:
            # Always 4 bytes
            self.header.to_level(writer, replace_byte_count=4)
        writer.write_s32(self.int_)

class MapProperty(BaseObject):
    def __init__(self, unknown, key_type, include_key_header, key_header_data, value_type, include_value_header, value_header_data, bytes_to_read, unknown2, count, map_data):
        self.unknown = unknown
        self.key_type = key_type
        self.include_key_header = include_key_header
        self.key_header_data = key_header_data
        self.value_type = value_type
        self.include_value_header = include_value_header
        self.value_header_data = value_header_data
        # self.bytes_to_read = bytes_to_read
        self.unknown2 = unknown2
        # self.count = count
        self.map_data = map_data

    @classmethod
    def from_level(cls, reader):
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

        map_data = {}
        if key_type in property_string_to_class and value_type in property_string_to_class:
            key_class = property_string_to_class[key_type]
            value_class = property_string_to_class[value_type]
            if key_class == IntProperty and value_class == StrProperty \
                or key_class == StructProperty and value_class == StructProperty:
                pass
            else:
                print(f"Warning! Untested map element types \"{key_type}\" & \"{value_type}\". Potential for incorrect parsing / crash")
            for _ in range(count):
                # TODO: Don't modify key/value types to make json.dumps happy, instead fix them in the to_dict method so the values are editable
                key = key_class.from_level(reader, include_header=False, header_data=key_header_data)
                # if isinstance(key, dict):
                #     # Used in one of the actor properties. intpoint struct. Needs to be updated to fit the rewrite
                #     # Convert it to a JSON string so it is hashable & python is happy
                #     key = json.dumps(key)
                value = value_class.from_level(reader, include_header=False, header_data=value_header_data)
                map_data.update({key: value})
        else:
            print(f"Unimplemented map type(s)!: @{reader.stream.tell():#2x} \"{key_type}\" || \"{value_type}\"")
            map_data.update({"data": base64.b64encode(reader.read_data(bytes_to_read - 8)).decode()})

        return cls(non_zero_unknown, key_type, include_key_header, key_header_data, value_type, include_value_header, value_header_data, bytes_to_read, non_zero_unknown2, count, map_data)

    def to_dict(self):
        ret = {}
        ret.update({"unknown" : self.unknown})
        ret.update({"key_type" : self.key_type})
        ret.update({"include_key_header" : self.include_key_header})
        if self.key_header_data:
            ret.update({"key_header_data" : self.key_header_data})
        ret.update({"value_type" : self.value_type})
        ret.update({"include_value_header" : self.include_value_header})
        if self.value_header_data:
            ret.update({"value_header_data" : self.value_header_data})
        # ret.update({"bytes_to_read" : self.bytes_to_read})
        ret.update({"unknown2" : self.unknown2})
        # ret.update({"count" : self.count})
        print(self.map_data)
        # Convert the map_data to something hashable by json.dumps
        new_map_data = {}
        for key, value in self.map_data.items():
            new_key = key.int_ if isinstance(key, IntProperty) else key
            new_value = value.string if isinstance(value, StrProperty) else value
            new_map_data.update({new_key : new_value})
        ret.update({"map_data" : new_map_data})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        unknown = dictionary["unknown"]
        key_type = dictionary["key_type"]
        include_key_header = dictionary["include_key_header"]
        key_header_data = None
        if "key_header_data" in dictionary:
            key_header_data = dictionary["key_header_data"]
        value_type = dictionary["value_type"]
        include_value_header = dictionary["include_value_header"]
        value_header_data = None
        if "value_header_data" in dictionary:
            value_header_data = dictionary["value_header_data"]
        # bytes_to_read = dictionary["bytes_to_read"]
        bytes_to_read = -1
        unknown2 = dictionary["unknown2"]
        # count = dictionary["count"]
        count = -1
        map_data = dictionary["map_data"]
        # Convert the map_data to the ?Property types
        new_map_data = {}
        for key, value in map_data.items():
            new_key = IntProperty(None, int(key)) if key_type == "IntProperty" else key
            new_value = StrProperty(None, value) if value_type == "StrProperty" else value
            new_map_data.update({new_key : new_value})
        return cls(unknown, key_type, include_key_header, key_header_data, value_type, include_value_header, value_header_data, bytes_to_read, unknown2, count, new_map_data)

    def to_level(self, writer):        
        count = len(self.map_data)

        writer.write_data(self.unknown.encode())
        writer.write_string(self.key_type)
        writer.write_u32(self.include_key_header)
        if self.key_header_data:
            if self.key_type == "StructProperty":
                StructProperty.unparse_separate_header(writer, magic=self.include_key_header, header_data=self.key_header_data)
            else:
                print(f"Warning! Unknown key with extra data! Key Type:\"{self.key_type}\". Probable crash")

        writer.write_string(self.value_type)
        writer.write_u32(self.include_value_header)
        if self.value_header_data:
            if self.value_type == "StructProperty":
                StructProperty.unparse_separate_header(writer, magic=self.include_value_header, header_data=self.value_header_data)
            else:
                print(f"Warning! Unknown key with extra data! Value Type:\"{self.value_type}\". Probable crash")

        # Calculate bytes dynamically
        byte_count_pos = writer.stream.tell()
        writer.write_u32(0x41414141)
        writer.write_data(self.unknown2.encode())
        byte_count_start = writer.stream.tell()

        writer.write_data(b'\x00' * 4)

        # Write map count based on element length
        writer.write_u32(count)

        for key, value in self.map_data.items():
            if self.key_type in property_string_to_class and self.value_type in property_string_to_class:
                if self.key_type == "IntProperty" and self.value_type == "StrProperty" or \
                    self.key_type == "StructProperty" and self.value_type == "StructProperty":
                    pass
                else:
                    print(f"Warning! Untested map element types \"{self.key_type}\" & \"{self.value_type}\". Potential for incorrect parsing / crash")
                
                # if isinstance(key, str):
                #     # TODO: Check the actor property caveat in from_level
                #     # Convert it from a JSON string so the struct unparser works
                #     key = json.loads(key)
                key.to_level(writer, include_header=False, header_data=self.key_header_data)
                value.to_level(writer, include_header=False, header_data=self.value_header_data)
            else:
                # TODO: Untested
                # Sanity check
                assert key == "data"
                writer.write_data(base64.b64decode(self.map_data[key].encode()))

        # Hacky fix for data length
        current_pos = writer.stream.tell()
        writer.stream.seek(byte_count_pos, os.SEEK_SET)
        if USE_LENGTHS:
            writer.write_u32(self.bytes)
        else:
            byte_count = current_pos - byte_count_start
            writer.write_u32(byte_count)
        writer.stream.seek(current_pos, os.SEEK_SET)

class NamedProperty(BaseObject):
    def __init__(self, name, property_type, property_object):
        self.name = name
        self.property_type = property_type
        self.property_object = property_object

    @classmethod
    def from_level(cls, reader):
        name = reader.read_string()
        if name == "None":
            reader.read_data(4)
            return None

        property_type = reader.read_string()

        if property_type in property_string_to_class:
            property_object = property_string_to_class[property_type].from_level(reader)
        elif name == "None":     # TODO: 4 bytes after "None" is a 0 length string
            pass
        else:
            print(f"Unimplemented named property type!: @{reader.stream.tell():#2x} \"{property_type}\"")
            # If type is unknown, assume it uses the common header
            header = CommonHeader.from_level(reader)
            data = reader.read_data(header["bytes"]).decode(encoding="unicode_escape")
            property_data = {
                "header": header,
                "data": data,
            }
            property_object = property_data     # TODO: Fix me

        # Hacky fix to parse the ActorProperties byte array. Needlessly specific (name is sufficient)
        # TODO: make this better later after rewrite
        if False:
            if name == "ActorProperties" and property_type == "ArrayProperty" and property_data["type"] == "ByteProperty":
                # Replace the byte array base64 data with the parsed script objects
                # Actor properties use a separate string cache (likely because Talos parses them after the main script)

                # TODO: Hacky (& potentially slow) fix for separate string caches
                global CACHED_STRINGS
                global SOFT_OBJECT_CACHED_STRINGS
                saved_cache = CACHED_STRINGS.copy()
                saved_soft_cache = SOFT_OBJECT_CACHED_STRINGS.copy()
                CACHED_STRINGS = []
                SOFT_OBJECT_CACHED_STRINGS = []

                actor_prop_bytes = base64.b64decode(property_data["elements"][0]["data"])
                actor_prop = Episode()
                actor_prop.from_in_memory(actor_prop_bytes)
                property_data["elements"][0]["data"] = actor_prop.episode

                CACHED_STRINGS = saved_cache.copy()
                SOFT_OBJECT_CACHED_STRINGS = saved_soft_cache.copy()

        return cls(name, property_type, property_object)

    def to_dict(self):
        ret = {}
        ret.update({"__type" : self.property_type})
        assert self.name
        if self.property_object:
            ret.update(self.property_object.to_dict())
        return {self.name : ret}

    @classmethod
    def from_dict(cls, name, data):
        property_type = data.pop("__type")
        if property_type in property_string_to_class:
            property_object = property_string_to_class[property_type].from_dict(data)
        elif name == "None":     # TODO: 4 bytes after "None" is a 0 length string
            pass
        else:
            print(f"Unimplemented named property type!: \"{property_type}\"")
            # If type is unknown, assume it uses the common header
            header = CommonHeader.from_dict(data["header"])
            data2 = data["data"]
            property_data = {
                "header": header,
                "data": data2,
            }
            property_object = property_data     # TODO: Fix me
        
        # Hacky fix to parse the ActorProperties byte array. Needlessly specific (name is sufficient)
        # TODO: make this better later after rewrite
        if False:
            if name == "ActorProperties" and property_type == "ArrayProperty" and property_data["type"] == "ByteProperty":
                # Replace the byte array base64 data with the parsed script objects
                # Actor properties use a separate string cache (likely because Talos parses them after the main script)

                # TODO: Hacky (& potentially slow) fix for separate string caches
                global CACHED_STRINGS
                global SOFT_OBJECT_CACHED_STRINGS
                saved_cache = CACHED_STRINGS.copy()
                saved_soft_cache = SOFT_OBJECT_CACHED_STRINGS.copy()
                CACHED_STRINGS = []
                SOFT_OBJECT_CACHED_STRINGS = []

                actor_prop_bytes = base64.b64decode(property_data["elements"][0]["data"])
                actor_prop = Episode()
                actor_prop.from_in_memory(actor_prop_bytes)
                property_data["elements"][0]["data"] = actor_prop.episode

                CACHED_STRINGS = saved_cache.copy()
                SOFT_OBJECT_CACHED_STRINGS = saved_soft_cache.copy()

        return cls(name, property_type, property_object)

    def to_level(self, writer):
        writer.write_string(self.name)
        writer.write_string(self.property_type)

        # Hacky fix to parse the ActorProperties byte array. Needlessly specific (name is sufficient)
        # TODO: make this better later after rewrite
        if False:
            if self.name == "ActorProperties" and self.property_type == "ArrayProperty" and self.property_object.element_type == "ByteProperty":
                # Replace the byte array base64 data with the parsed script objects
                # Actor properties use a separate string cache (likely because Talos parses them after the main script)
                
                # TODO: Hacky (& potentially slow) fix for separate string caches
                global CACHED_STRINGS
                global SOFT_OBJECT_CACHED_STRINGS
                saved_cache = CACHED_STRINGS.copy()
                saved_soft_cache = SOFT_OBJECT_CACHED_STRINGS.copy()
                CACHED_STRINGS = []
                SOFT_OBJECT_CACHED_STRINGS = []
                
                actor_prop = Episode()
                actor_prop.episode = self.property_object["elements"][0]["data"]
                actor_prop_bytes = b''
                actor_prop_bytes = actor_prop.to_in_memory()
                self.property_object["elements"][0]["data"] = base64.b64encode(actor_prop_bytes).decode()

                CACHED_STRINGS = saved_cache.copy()
                SOFT_OBJECT_CACHED_STRINGS = saved_soft_cache.copy()

        if self.property_type in property_string_to_class:
            self.property_object.to_level(writer)
        else:
            print(f"Unimplemented named property type!: \"{self.property_type}\"")
            # If type is unknown, assume it uses the common header
            # TODO: Does this work?
            assert False
            self.property_object["header"].to_level(writer)
            writer.write_data(self.property_object["data"].encode())

class ObjectProperty(BaseObject):
    def __init__(self, header, object_):
        self.header = header
        self.object_ = object_

    @classmethod
    def from_level(cls, reader, include_header=True, header_data=None):
        header = CommonHeader.from_level(reader) if include_header else None
        obj = ScriptObject.from_level(reader)
        return cls(header, obj)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header" : self.header.to_dict()})
        ret.update(self.object_.to_dict())
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary.pop("header"))
        object_ = ScriptObject.from_dict(dictionary)
        return cls(header, object_)

    def to_level(self, writer, include_header=True, header_data=None):
        if include_header:
            # Put the header in, but remember the position so we can update the length later
            header_pos = writer.stream.tell()
            self.header.to_level(writer, replace_byte_count=0x41414141)
            byte_count_start = writer.stream.tell()
        self.object_.to_level(writer)

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if USE_LENGTHS:
                self.header.to_level(writer)
            else:
                byte_count = current_pos - byte_count_start
                self.header.to_level(writer, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class ScriptObject(BaseObject):
    def __init__(self, special, user_content_type, object_path, cache_index, named_properties):
        self.special = special
        self.user_content_type = user_content_type
        self.object_path = object_path
        self.cache_index = cache_index
        self.named_properties = named_properties

    @classmethod
    def from_level(cls, reader):
        special = reader.read_u8()
        if special == 0:
            # Object ends
            return {"special": special,}
        if special == 0x02:
            user_content_type = reader.read_u8()
        else:
            user_content_type = special
       
        if user_content_type == 0x08:   # Uncached string
            object_path = reader.read_string()
            cache_index = len(CACHED_STRINGS)
            CACHED_STRINGS.append(object_path)
        elif user_content_type == 0x07:   # Asset path
            # TODO: Unsure if this gets cached (or even uses the same cache)
            object_path = reader.read_string()
            cache_index = -1
        elif user_content_type == 0x04:  # Cached string
            cache_index = reader.read_u32()
            object_path = CACHED_STRINGS[cache_index]
            # object_path = ""        # TODO: Remove unneccisary/confusing elements
        elif user_content_type == 0x03:  # When targeting another actor. I assume the following u32 is the actor index
            object_path = "Target Actor Index"
            cache_index = reader.read_u32()     # use cache_index to avoid adding new elements
        else:
            print(f"Unimplemented user content type: {user_content_type:#2x}")
            object_path = "Unimplemented path type"
            cache_index = -1

        # Extra padding sometimes, noticed it's the case when `special` == 0x2
        if special == 0x2:
            reader.read_data(1)

        # Guessing this determines if there are named properties
        named_properties = []
        if special == 0x02:
            while True:
                prop = NamedProperty.from_level(reader)
                if prop == None:
                    break
                named_properties.append(prop)
        elif special == 0x08 or special == 0x07 or special == 0x03 or special == 0x4:
            # No named properties
            pass
        else:
            print(f"Unknown special value: {special:#2x}")

        return cls(special, user_content_type, object_path, cache_index, named_properties)

    def to_dict(self):
        ret = {}
        ret.update({"special" : self.special})
        ret.update({"user_content_type" : self.user_content_type})
        if self.object_path:
            ret.update({"object_path" : self.object_path})
        if self.cache_index != -1:
            ret.update({"cache_index" : self.cache_index})
        if self.named_properties:
            named_properties = {}
            for prop in self.named_properties:
                named_properties.update(prop.to_dict())
            ret.update({"named_properties" : named_properties})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        special = dictionary["special"]
        user_content_type = dictionary["user_content_type"]

        object_path = ""
        if "object_path" in dictionary:
            object_path = dictionary["object_path"]
        cache_index = -1
        if "cache_index" in dictionary:
            cache_index = dictionary["cache_index"]
        named_properties = []
        if "named_properties" in dictionary:
            properties = dictionary["named_properties"]
            for name, data in properties.items():
                named_properties.append(NamedProperty.from_dict(name, data))
        
        return cls(special, user_content_type, object_path, cache_index, named_properties)

    def to_level(self, writer):
        writer.write_u8(self.special)
        if self.special == 0:
            # Object ends
            return

        # Extra padding sometimes, noticed it's the case when `special` == 0x2
        if self.special == 0x02:
            writer.write_u8(self.user_content_type)

        if self.user_content_type == 0x08 or self.user_content_type == 0x07:
            writer.write_string(self.object_path)
        elif self.user_content_type == 0x04 or self.user_content_type == 0x03:
            writer.write_u32(self.cache_index)

        if self.special == 0x02:
            writer.write_data(b'\x00')
        
        if self.special == 0x02:
            for prop in self.named_properties:
                prop.to_level(writer)
            # Write the `None` property
            writer.write_string("None")
            writer.write_data(b'\x00' * 4)

# class SoftObjectProperty(object):
#     def from_level(reader, include_header=True):
#         global SOFT_OBJECT_CACHED_STRINGS
#         ret = dict()
#         if include_header:
#             header = CommonHeader.from_level(reader)
#             ret.update({"header": header})
        
#         special = reader.read_u8()
#         if special == 0x09:     # Mesh reference
#             object_path = reader.read_string()
#             object_index = reader.read_s32()
#             cache_index = len(SOFT_OBJECT_CACHED_STRINGS)
#             SOFT_OBJECT_CACHED_STRINGS.append(object_path)
#             object_name = ""
#         elif special == 0x07:   # Collision reference
#             # Unsure if it is cached
#             object_path = reader.read_string()
#             object_name = reader.read_string()
#             reader.read_data(4)     # Unknown
#             cache_index = -1
#             object_index = -1
#         elif special == 0x06:   # Cached Mesh reference
#             cache_index = reader.read_s32()
#             object_path = SOFT_OBJECT_CACHED_STRINGS[cache_index]
#             object_index = reader.read_s32()
#             object_name = ""
#         else:
#             print(f"Unimplemented soft object special: {special:#2x}")
#             object_path = "Unimplemented path type"
#             object_name = "Unimplemented path type"
#             object_index = -1
#             cache_index = -1

#         obj = {
#             "special": special,
#             "cache_index": cache_index,
#             "object_path": object_path,
#             "object_name": object_name,
#             "object_index": object_index,
#         }

#         ret.update({"soft_object": obj,})
#         return ret

#     def to_level(self, writer, include_header=True):
#         if include_header:
#             header_pos = writer.stream.tell()
#             CommonHeader.to_level(writer, data["header"])
#             byte_count_start = writer.stream.tell()

#         obj = data["soft_object"]

#         special = obj["special"]
#         object_path = obj["object_path"]
#         object_name = obj["object_name"]
#         object_index = obj["object_index"]
#         cache_index = obj["cache_index"]

#         writer.write_u8(special)
#         if special == 0x09:     # Mesh reference
#             writer.write_string(object_path)
#             writer.write_s32(object_index)
#         elif special == 0x07:   # Editor Collision reference
#             writer.write_string(object_path)
#             writer.write_string(object_name)
#             writer.write_data(b'\x00' * 4)
#         elif special == 0x06:   # Cached Mesh reference
#             writer.write_s32(cache_index)
#             writer.write_s32(object_index)

#         if include_header:
#             header = data["header"]
#             # Calculate bytes dynamically
#             current_pos = writer.stream.tell()
#             writer.stream.seek(header_pos, os.SEEK_SET)
#             if USE_LENGTHS:
#                 CommonHeader.to_level(writer, header)
#             else:
#                 byte_count = current_pos - byte_count_start
#                 CommonHeader.to_level(writer, header, replace_byte_count=byte_count)
#             writer.stream.seek(current_pos, os.SEEK_SET)

class StrProperty(BaseObject):
    def __init__(self, header, string):
        self.header = header
        self.string = string

    @classmethod
    def from_level(cls, reader, include_header=True, header_data=None):
        header = CommonHeader.from_level(reader) if include_header else None
        string = reader.read_string()
        return cls(header, string)

    def to_dict(self):
        ret = {}
        if self.header:
            ret.update({"header" : self.header.to_dict()})
        ret.update({"string" : self.string})
        return ret

    @classmethod
    def from_dict(cls, dictionary, include_header=True):
        header = CommonHeader.create_empty() if include_header else None
        if "header" in dictionary:
            header = CommonHeader.from_dict(dictionary["header"])
        string = dictionary["string"]
        return cls(header, string)

    def to_level(self, writer, include_header=True, header_data=None):
        if include_header:
            header_pos = writer.stream.tell()
            self.header.to_level(writer, replace_byte_count=0x41414141)
            byte_count_start = writer.stream.tell()

        writer.write_string(self.string)

        if include_header:
            # Calculate bytes dynamically
            current_pos = writer.stream.tell()
            writer.stream.seek(header_pos, os.SEEK_SET)
            if USE_LENGTHS:
                self.header.to_level(writer)
            else:
                byte_count = current_pos - byte_count_start
                self.header.to_level(writer, replace_byte_count=byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

class StructProperty(BaseObject):
    def __init__(self, magic, struct_name, unknown, path, magic_unknown, uuid, byte_count, unknown2, data):
        self.magic = magic
        self.struct_name = struct_name
        self.unknown = unknown
        self.path = path
        self.magic_unknown = magic_unknown
        self.uuid = uuid
        # self.byte_count = byte_count
        self.unknown2 = unknown2
        self.data = data

    @classmethod
    def from_level(cls, reader, include_header=True, header_data=None):
        if include_header:
            magic = reader.read_u32()
            struct_name = reader.read_string()
            non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
            path = reader.read_string()

            magic_unknown = ""
            uuid = ""
            if magic == 2:
                magic_unknown = reader.read_data(4).decode(encoding="unicode_escape")
                uuid = reader.read_string()

            reader.read_data(4)     # Unknown
            bytes_to_read = reader.read_u32()
            non_zero_unknown2 = reader.read_data(1).decode(encoding="unicode_escape")     # Unknown
        else:
            # In arrays/maps the struct header exists in the array/map header
            # Make sure the header data is passed as a argument
            assert header_data

        if header_data:
            magic = header_data["magic"]
            struct_name = header_data["struct_name"]
            non_zero_unknown = header_data["non_zero_unknown"]
            path = header_data["path"]
            magic_unknown = header_data["magic_unknown"]
            uuid = header_data["uuid"]
            bytes_to_read = -1
            non_zero_unknown2 = -1

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
            named_properties = []
            while True:
                prop = NamedProperty.from_level(reader)
                if prop == None:
                    # Hack for None type having no extra bytes
                    reader.stream.seek(-4, os.SEEK_CUR)
                    break
                named_properties.append(prop)
            data = named_properties
        return cls(magic, struct_name, non_zero_unknown, path, magic_unknown, uuid, bytes_to_read, non_zero_unknown2, data)

    def parse_separate_header(reader, magic):
        struct_name = reader.read_string()
        non_zero_unknown = reader.read_data(4).decode(encoding="unicode_escape")     # Unknown
        path = reader.read_string()

        magic_unknown = ""
        uuid = ""
        if magic == 2:
            magic_unknown = reader.read_data(4).decode(encoding="unicode_escape")
            uuid = reader.read_string()

        reader.read_data(4)     # Unknown
        
        return {
            "magic": magic,
            "struct_name": struct_name,
            "non_zero_unknown": non_zero_unknown,
            "path": path,
            "magic_unknown": magic_unknown,
            "uuid": uuid,
        }

    def to_dict(self):
        ret = {}
        ret.update({"magic" : self.magic})
        ret.update({"struct_name" : self.struct_name})
        ret.update({"unknown" : self.unknown})
        ret.update({"path" : self.path})
        if self.magic_unknown:
            ret.update({"magic_unknown" : self.magic_unknown})
        if self.uuid:
            ret.update({"uuid" : self.uuid})
        # ret.update({"byte_count" : self.byte_count})
        ret.update({"unknown2" : self.unknown2})
        if self.struct_name in ["Vector", "Quat", "IntPoint", "Rotator", "LinearColor"]:
            ret.update(self.data)
        else:       # Custom struct, not part of core Unreal Engine
            named_properties = {}
            for prop in self.data:
                named_properties.update(prop.to_dict())
            ret.update({"data" : named_properties})
        return ret

    @classmethod
    def from_dict(cls, dictionary):
        magic = dictionary["magic"]
        struct_name = dictionary["struct_name"]
        unknown = dictionary["unknown"]
        path = dictionary["path"]
        magic_unknown = ""
        if "magic_unknown" in dictionary:
            magic_unknown = dictionary["magic_unknown"]
        uuid = ""
        if "uuid" in dictionary:
            uuid = dictionary["uuid"]
        # byte_count = dictionary["byte_count"]
        byte_count = -1
        unknown2 = dictionary["unknown2"]
        if struct_name == "Vector":
            data = dictionary["vector"]
        elif struct_name == "Quat":
            data = dictionary["quat"]
        elif struct_name == "IntPoint":
            data = dictionary["intpoint"]
        elif struct_name == "Rotator":
            data = dictionary["rotator"]
        elif struct_name == "LinearColor":
            data = dictionary["colour"]
        else:       # Custom struct, not part of core Unreal Engine
            named_properties = []
            for name, data in dictionary["data"].items():
                print(name)
                print(data)
                named_properties.append(NamedProperty.from_dict(name, data))
            data = named_properties
        return cls(magic, struct_name, unknown, path, magic_unknown, uuid, byte_count, unknown2, data)

    def to_level(self, writer, include_header=True, header_data=None):
        if include_header:
            writer.write_u32(self.magic)
            writer.write_string(self.struct_name)
            writer.write_data(self.unknown.encode())
            writer.write_string(self.path)
            if self.magic == 2:
                if self.magic_unknown:
                    writer.write_data(self.magic_unknown.encode())
                else:
                    writer.write_data(b'\x00' * 4)
                writer.write_string(self.uuid)
            writer.write_data(b'\x00' * 4)

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
            writer.write_data(struct.pack("<4f", colour[0], colour[1], colour[2], colour[3]))
        else:
            for prop in self.data:
                prop.to_level(writer)
            # Write the `None` property
            writer.write_string("None")

        # Hacky fix for data length
        if include_header:
            current_pos = writer.stream.tell()
            writer.stream.seek(byte_count_pos, os.SEEK_SET)
            if USE_LENGTHS:
                # TODO: Remove??
                # writer.write_u32(data["bytes"])
                writer.write_u32(0x41424344)
            else:
                byte_count = current_pos - byte_count_start
                writer.write_u32(byte_count)
            writer.stream.seek(current_pos, os.SEEK_SET)

    def unparse_separate_header(writer, magic, header_data):
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
                writer.write_data(b'\x00' * 4)
            writer.write_string(uuid)
        writer.write_data(b'\x00' * 4)

# class TextProperty(BaseObject):
#     def from_level(reader, include_header=True):
#         ret = dict()
#         if include_header:
#             header = CommonHeader.from_level(reader)
#             ret.update({"header": header})
#         unknown1 = reader.read_u32()
#         assert unknown1 == 0x12
#         unknown2 = reader.read_u8()
#         assert unknown2 == 0xff
#         text_exists = reader.read_u32()
#         text = ""
#         if text_exists == 0x1:
#             text = reader.read_string()
#         else:
#             assert text_exists == 0x0
#         ret.update({
#             "unknown1": unknown1,
#             "unknown2": unknown2,
#             "text": text,
#         })
#         return ret

#     def to_level(self, writer, include_header=True):
#         if include_header:
#             text = data["text"]

#             header_pos = writer.stream.tell()
#             CommonHeader.to_level(writer, data["header"], replace_byte_count=0x41414141)
#             byte_count_start = writer.stream.tell()
#         else:
#             text = data
        
#         unknown1 = data["unknown1"]
#         unknown2 = data["unknown2"]

#         writer.write_u32(unknown1)
#         writer.write_u8(unknown2)
#         if len(text) > 0:
#             writer.write_u32(0x1)
#             writer.write_string(text)
#         else:
#             writer.write_u32(0x0)

#         # Hacky fix for utf-16 strings
#         if include_header:
#             header = data["header"]
#             # Calculate bytes dynamically
#             current_pos = writer.stream.tell()
#             writer.stream.seek(header_pos, os.SEEK_SET)
#             if USE_LENGTHS:
#                 CommonHeader.to_level(writer, header)
#             else:
#                 byte_count = current_pos - byte_count_start
#                 CommonHeader.to_level(writer, header, replace_byte_count=byte_count)
#             writer.stream.seek(current_pos, os.SEEK_SET)

class Episode:
    def __init__(self, scripts, cached_strings):
        self.scripts = scripts
        self.cached_strings = cached_strings

    @classmethod
    def from_episode(cls, episode_path):
        global CACHED_STRINGS
        assert not CACHED_STRINGS       # TODO: CACHED_STRINGS should be empty??
        scripts = []
        with open(episode_path, "rb") as f:
            reader = BinaryReader(f)

            # Unknown what the first 8 bytes are. Always 0
            reader.read_data(8)

            # Read scripts until there are no more bytes
            while reader.stream.peek(1):
                script = ScriptObject.from_level(reader)
                scripts.append(script)
        cached_strings = CACHED_STRINGS
        CACHED_STRINGS = []
        return cls(scripts, cached_strings)

    @classmethod
    def from_json(cls, json_path):
        global CACHED_STRINGS
        assert not CACHED_STRINGS       # TODO: CACHED_STRINGS should be empty??
        with open(json_path, "rb") as f:
            episode = json.loads(f.read())
        CACHED_STRINGS = episode["cached_strings"]
        scripts = []
        for script_dict in episode["scripts"]:
            script = ScriptObject.from_dict(script_dict)
            scripts.append(script)
        cached_strings = CACHED_STRINGS
        CACHED_STRINGS = []
        return cls(scripts, cached_strings)

    def from_in_memory(self, bytes_data):
        with io.BytesIO(bytes_data) as buffer:
            buf_reader = io.BufferedReader(buffer)
            self.reader = BinaryReader(buf_reader)

            # Unknown what the first 8 bytes are. Always 0
            self.reader.read_data(8)

            scripts = []
            # Read scripts until there are no more bytes
            while self.reader.stream.peek(1):
                script = ScriptObject.from_level(self.reader)
                scripts.append(script)

            self.episode["episode"] = scripts

    def to_json(self, json_path):
        dicts = []
        for script in self.scripts:
            dicts.append(script.to_dict())
        episode = {
            "cached_strings" : self.cached_strings,
            "scripts" : dicts,
        }
        with open(json_path, "wb") as f:
            f.write(json.dumps(episode, indent=2).encode())

    def to_episode(self, episode_path):
        with open(episode_path, "wb") as f:
            writer = BinaryWriter(f)

            # Unknown what the first 8 bytes are. Always 0
            writer.write_data(b'\x00' * 8)

            for script in self.scripts:
                script.to_level(writer)

    def to_in_memory(self):
        with io.BytesIO() as buffer:
            buf_writer = io.BufferedWriter(buffer)
            self.writer = BinaryWriter(buf_writer)

            # Unknown what the first 8 bytes are. Always 0
            self.writer.write_data(b'\x00' * 8)

            for script in self.episode["episode"]:
                ScriptObject.to_level(self.writer, script)
            
            self.writer.stream.flush()
            return buffer.getvalue()

    def dump(self):
        print(json.dumps(self.episode, indent=2))

# List of classes that I have tested with array
# New classes may have issues with headers
TESTED_ARRAY_CLASSES = [
    BoolProperty,
    # EnumProperty,
    IntProperty,
    ObjectProperty,
    StructProperty,
    StrProperty,
]

property_string_to_class = {
        "ArrayProperty": ArrayProperty,
        "BoolProperty": BoolProperty,
        # "ByteProperty": ByteProperty,
        # "EnumProperty": EnumProperty,
        "FloatProperty": FloatProperty,
        # "DoubleProperty": DoubleProperty,
        "IntProperty": IntProperty,
        "MapProperty": MapProperty,
        "ObjectProperty": ObjectProperty,
        # "SoftObjectProperty": SoftObjectProperty,
        "StrProperty": StrProperty,
        "NameProperty": StrProperty,        # Acts like a string. Only seen in sign level targets (which doesn't use the user given name)
        "StructProperty": StructProperty,
        # "TextProperty": TextProperty,
    }

# TODO: Parse the actor properties
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
    parser.add_argument("--use_lengths", action="store_true", help="If set, will use the data lengths found in the JSON. Otherwise, will ignore data lengths can calculate them dynamically")

    # .level & .episode files are largely handled by Unreal Engine, with each script/object having a `Serialize` function.
    # This results in a file format similar to unreal games saves (GVAS). Its possible those tool can read/edit .episode & .level files
    # However, "Puzzle Editor author did do a lot of customization as to how the custom levels are serialized" (https://discord.com/channels/464411560563965953/1315739667202834484/1359821476093624383)
    
    # This tool can read .level files!!

    args = parser.parse_args()
    input_path = args.input_file
    output_path = args.output
    use_episode_extension = args.episode

    global USE_LENGTHS
    if args.use_lengths:
        USE_LENGTHS = args.use_lengths
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

    # e = Episode()
    assert not USE_LENGTHS
    if convert_type == "from_json":
        e = Episode.from_json(input_path)
        print(e.scripts)
        e.to_episode(output_path)
    elif convert_type == "to_json":
        e = Episode.from_episode(input_path)
        print(e.scripts)
        e.to_json(output_path)


if __name__ == "__main__":
    main()