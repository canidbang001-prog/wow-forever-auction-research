"""Data-only Lua reader + CBOR decoder for the inspected Auctionator v345 file.

No eval, Lua runtime, game process access, or inferred price field names.
"""
import re
import struct
from common import AppError


class LuaData:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def skip(self):
        while self.pos < len(self.data):
            if self.data[self.pos:self.pos+2] == b"--":
                end = self.data.find(b"\n", self.pos)
                self.pos = len(self.data) if end < 0 else end + 1
            elif self.data[self.pos] in b" \r\n\t":
                self.pos += 1
            else:
                break

    def take(self, expected):
        self.skip()
        if self.data[self.pos:self.pos+len(expected)] != expected:
            raise AppError("지원하지 않는 Lua 데이터 구조")
        self.pos += len(expected)

    def value(self, depth=0):
        if depth > 64:
            raise AppError("SavedVariables 중첩 한도 초과")
        self.skip()
        if self.pos >= len(self.data):
            raise AppError("잘린 SavedVariables 파일")
        c = self.data[self.pos]
        if c in (34, 39):
            self.pos += 1
            out = bytearray()
            escapes = {97:7, 98:8, 102:12, 110:10, 114:13, 116:9, 118:11,
                       92:92, 34:34, 39:39, 10:10}
            while self.pos < len(self.data):
                ch = self.data[self.pos]
                self.pos += 1
                if ch == c:
                    return bytes(out)
                if ch == 92:
                    if self.pos >= len(self.data):
                        break
                    ch = self.data[self.pos]
                    if 48 <= ch <= 57:
                        m = re.match(rb"\d{1,3}", self.data[self.pos:])
                        val = int(m[0])
                        if val > 255:
                            raise AppError("Lua 바이트 이스케이프 범위 오류")
                        out.append(val)
                        self.pos += len(m[0])
                        continue
                    if ch not in escapes:
                        raise AppError("지원하지 않는 Lua 문자열 이스케이프")
                    self.pos += 1
                    ch = escapes[ch]
                out.append(ch)
            raise AppError("잘린 Lua 문자열")
        if c == 123:
            self.pos += 1
            result, index = {}, 1
            while True:
                self.skip()
                if self.data[self.pos:self.pos+1] == b"}":
                    self.pos += 1
                    return result
                if self.data[self.pos:self.pos+1] == b"[":
                    self.pos += 1
                    key = self.value(depth+1)
                    if isinstance(key, bytes):
                        key = key.decode("utf-8")
                    self.take(b"]")
                    self.take(b"=")
                else:
                    key, index = index, index+1
                result[key] = self.value(depth+1)
                self.skip()
                if self.data[self.pos:self.pos+1] in (b",", b";"):
                    self.pos += 1
                elif self.data[self.pos:self.pos+1] != b"}":
                    raise AppError("Lua 테이블 구분자 오류")
        for literal, val in ((b"true", True), (b"false", False), (b"nil", None)):
            if self.data[self.pos:self.pos+len(literal)] == literal:
                self.pos += len(literal)
                return val
        m = re.match(rb"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", self.data[self.pos:])
        if m:
            self.pos += len(m[0])
            return float(m[0]) if any(c in m[0] for c in b".eE") else int(m[0])
        raise AppError("실행 가능한 Lua 또는 미지원 값: 읽기 중단")


class CBOR:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise AppError("잘린 CBOR 데이터")
        result = self.data[self.pos:self.pos+size]
        self.pos += size
        return result

    def value(self, depth=0):
        if depth > 64:
            raise AppError("CBOR 중첩 한도 초과")
        initial = self.read(1)[0]
        major, add = initial >> 5, initial & 31
        if major == 7:
            if add in (20,21,22):
                return {20:False,21:True,22:None}[add]
            if add in (25,26,27):
                return struct.unpack({25:">e",26:">f",27:">d"}[add], self.read(2**(add-24)))[0]
            raise AppError("지원하지 않는 CBOR 단순값")
        if add < 24:
            length = add
        elif add <= 27:
            length = int.from_bytes(self.read(2**(add-24)), "big")
        else:
            raise AppError("지원하지 않는 CBOR 길이 형식")
        if major == 0:
            return length
        if major == 1:
            return -1-length
        if major in (2,3):
            data = self.read(length)
            return data if major == 2 else data.decode("utf-8")
        if major in (4,5):
            if length > len(self.data)-self.pos:
                raise AppError("CBOR 항목 수 범위 오류")
            if major == 4:
                return [self.value(depth+1) for _ in range(length)]
            out = {}
            for _ in range(length):
                key = self.value(depth+1)
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                out[key] = self.value(depth+1)
            return out
        raise AppError("지원하지 않는 CBOR 타입")


def decode_cbor(data):
    reader = CBOR(data)
    result = reader.value()
    if reader.pos != len(data):
        raise AppError("CBOR 뒤에 미해석 데이터가 있습니다.")
    return result


def price_database(raw):
    match = re.search(rb"(?m)^AUCTIONATOR_PRICE_DATABASE\s*=\s*", raw)
    if not match:
        raise AppError("AUCTIONATOR_PRICE_DATABASE를 찾지 못했습니다.")
    reader = LuaData(raw[match.end():])
    database = reader.value()
    if not isinstance(database, dict) or database.get("__dbversion") != 8:
        raise AppError("확인된 Auctionator DB 버전 8만 지원합니다.")
    for key, value in list(database.items()):
        if key == "__dbversion":
            continue
        if isinstance(value, bytes):
            value = decode_cbor(value)
        if not isinstance(value, dict) or value.get("version") != 2:
            raise AppError("확인된 realm 데이터 버전 2만 지원합니다.")
        database[key] = value
    return database
