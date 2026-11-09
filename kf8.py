#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KF8（azw3）解析模块（零第三方依赖，仅用标准库 struct / re）。

设计目标：在树莓派等无第三方库环境中，把 KF8 格式的 azw3 解析为
  - 一组合法的 XHTML 正文分片（parts）；
  - 带真实中文标题的目录（toc，标题取自 NCX 的 CTOC，而非条目编号）；
  - kindle:embed 图片资源的按需映射（get_image，不预抽取）。

整体链路（已用真实文件端到端验证）：
  Palm DB 记录 -> 按压缩方式解压（Huffman/PalmDOC/无压缩）拼 rawML
    -> FDST/SKEL/FRAG 索引把碎片重组为 XHTML 分片
    -> NCX 索引解析目录；资源槽映射图片。

仅支持无加密的 KF8；HD 双格式容器、Print Replica、DRM 加密不在本模块范围。
"""

import struct


class KF8Error(Exception):
    """KF8 解析相关错误：不支持的格式、加密或文件损坏。"""


# 图片格式 -> HTTP MIME
_IMAGE_MIME = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Palm 数据库（PalmDOC 容器）：记录偏移与属性
# ---------------------------------------------------------------------------
class PalmDB:
    """读取 Palm 数据库的记录分区信息。

    Palm DB 头部偏移 76 处为记录数（2 字节）；从 78 起每条记录 8 字节，
    前 4 字节为记录在文件中的绝对偏移，后 4 字节的低 24 位为唯一 ID。
    """

    def __init__(self, data):
        self.data = data
        count = struct.unpack_from(">H", data, 76)[0]
        self.count = count
        # 一次解出 count*2 个无符号长字，按奇偶取偏移/属性
        raw = struct.unpack_from(">%dL" % (count * 2), data, 78)
        self.offs = list(raw[0::2])
        self.attrs = list(raw[1::2])
        self.offs.append(len(data))     # 末尾哨兵，便于切片取最后一条

    def load(self, i):
        """返回第 i 条记录的字节内容。"""
        return self.data[self.offs[i]:self.offs[i + 1]]

    def uid(self, i):
        """第 i 条记录的唯一 ID（属性低 24 位）。"""
        return self.attrs[i] & 0xFFFFFF


# ---------------------------------------------------------------------------
# 解压器
# ---------------------------------------------------------------------------
class UncompressedReader:
    """无压缩（comp=1）：原样返回。"""

    def unpack(self, data):
        return data


class PalmdocReader:
    """PalmDOC LZ77 解压（comp=2），与 Kindle PalmDOC 标准对齐。

    逐字节读取，首字节分四个区间：
      1..8   ：字面量，紧随其后的 c 个字节原样输出；
      <128   ：单字节字面量；
      >=192  ：空格 + 字节（首字节异或 128）；
      其余   ：两字节组成的距离/长度对，从已输出窗口回拷。
    """

    def unpack(self, data):
        out, p, n = b"", 0, len(data)
        while p < n:
            c = data[p]
            p += 1
            if 1 <= c <= 8:
                out += data[p:p + c]
                p += c
            elif c < 128:
                out += bytes([c])
            elif c >= 192:
                out += b" " + bytes([c ^ 128])
            else:
                if p >= n:
                    break
                c = (c << 8) | data[p]
                p += 1
                dist = (c >> 3) & 0x07FF          # 回拷距离
                length = (c & 7) + 3              # 回拷长度
                if dist > length:
                    out += out[-dist:length - dist]
                else:
                    # 距离小于长度时逐字节回拷（允许重复同字节）
                    for _ in range(length):
                        if dist == 1:
                            out += out[-1:]
                        else:
                            out += out[-dist:-dist + 1]
        return out


class HuffcdicReader:
    """KF8 主流的 Huffman + CDIC 字典解压（comp=0x4448）。

    HUFF 记录给出两级码表（dict1 按首字节 256 项、dict2 按码长），
    CDIC 记录给出短语切片；位流按码长逐位取码，遇到未展开的短语递归解压。
    """

    _qword = struct.Struct(">Q").unpack_from

    def load_huff(self, huff):
        if huff[0:8] != b"HUFF\x00\x00\x00\x18":
            raise KF8Error("无效的 HUFF 头")
        off1, off2 = struct.unpack_from(">LL", huff, 8)

        def dict1_unpack(v):
            codelen, term, maxcode = v & 0x1F, v & 0x80, v >> 8
            # maxcode 规范化成 32 位码空间的上界
            maxcode = ((maxcode + 1) << (32 - codelen)) - 1
            return (codelen, term, maxcode)

        self.dict1 = list(map(dict1_unpack,
                              struct.unpack_from(">256L", huff, off1)))

        dict2 = struct.unpack_from(">64L", huff, off2)
        self.mincode, self.maxcode = (), ()
        for codelen, mincode in enumerate((0,) + dict2[0::2]):
            self.mincode += (mincode << (32 - codelen),)
        for codelen, maxcode in enumerate((0,) + dict2[1::2]):
            self.maxcode += (((maxcode + 1) << (32 - codelen)) - 1,)
        self.dictionary = []

    def load_cdic(self, cdic):
        if cdic[0:8] != b"CDIC\x00\x00\x00\x10":
            raise KF8Error("无效的 CDIC 头")
        phrases, bits = struct.unpack_from(">LL", cdic, 8)
        n = min(1 << bits, phrases - len(self.dictionary))
        h = struct.Struct(">H").unpack_from

        def get_slice(off):
            (blen,) = h(cdic, 16 + off)
            slice_ = cdic[18 + off:18 + off + (blen & 0x7FFF)]
            return (slice_, blen & 0x8000)       # 末位为已展开标志

        self.dictionary += list(map(
            get_slice, struct.unpack_from(">%dH" % n, cdic, 16)))

    def unpack(self, data):
        qword = HuffcdicReader._qword
        bits_left = len(data) * 8
        data = data + b"\x00" * 8                # 补零防止越位读取
        pos, n, s = 0, 32, b""
        (x,) = qword(data, pos)
        while True:
            if n <= 0:                           # 当前字取完，前移再读
                pos += 4
                (x,) = qword(data, pos)
                n += 32
            code = (x >> n) & 0xFFFFFFFF
            codelen, term, maxcode = self.dict1[code >> 24]
            if not term:                         # 非终结码，按码长细化
                while code < self.mincode[codelen]:
                    codelen += 1
                maxcode = self.maxcode[codelen]
            n -= codelen
            bits_left -= codelen
            if bits_left < 0:                    # 超出实际位流，结束
                break
            r = (maxcode - code) >> (32 - codelen)
            slice_, flag = self.dictionary[r]
            if not flag:                         # 短语尚未展开，先递归
                self.dictionary[r] = None
                slice_ = self.unpack(slice_)
                self.dictionary[r] = (slice_, 1)
            s += slice_
        return s


# ---------------------------------------------------------------------------
# 通用 INDX 索引解析（NCX / SKEL / FRAG 复用）
# ---------------------------------------------------------------------------
class MobiIndex:
    """解析 KF8 的 INDX 索引记录。

    一个索引由一条 INDX 头 + CTOC 文本记录 + TAGX 标签定义 + 若干条目记录组成。
    构造时传入 load 函数（通常是 PalmDB.load），用于按需读取记录。
    """

    # INDX 头中从偏移 4 开始的 13 个无符号长字字段名
    _WORDS = ["len", "nul1", "type", "gen", "start", "count", "code",
              "lng", "total", "ordt", "ligt", "nligt", "nctoc"]

    def __init__(self, load):
        self.load = load

    @staticmethod
    def _varwidth(d, off):
        """读取一个变长无符号整数：每字节低 7 位有效，最高位为结束标志。"""
        val, consumed = 0, 0
        while True:
            v = d[off + consumed]
            consumed += 1
            val = (val << 7) | (v & 0x7F)
            if v & 0x80:
                return consumed, val

    def _parse_indx(self, d):
        vals = struct.unpack(
            ">%dL" % len(self._WORDS), d[4:4 * (len(self._WORDS) + 1)])
        return dict(zip(self._WORDS, vals))

    def _read_ctoc(self, d):
        """读取 CTOC 文本块：变长长度 + 文本，返回 {记录内偏移: 文本字节}。"""
        out, off = {}, 0
        while off < len(d) and d[off] != 0:
            used, ilen = self._varwidth(d, off)
            off += used
            out[off - used] = d[off:off + ilen]
            off += ilen
        return out

    @staticmethod
    def _read_tag_section(start, d, u32):
        """读取 TAGX 标签定义区，返回 (控制字节数, [(tag,掩码宽,掩码,结束标志)])。"""
        cbc, tags = 0, []
        if d[start:start + 4] == b"TAGX":
            feo = u32(d, start + 4)
            cbc = u32(d, start + 8)
            for i in range(12, feo, 4):
                p = start + i
                tags.append((d[p], d[p + 1], d[p + 2], d[p + 3]))
        return cbc, tags

    @staticmethod
    def _count_bits(v):
        return bin(v).count("1")

    def _get_tag_map(self, cbc, tag_table, d, sp, ep):
        """解析单条目的标签位图，按 TAGX 定义抽出各 tag 的值列表。"""
        tlist, hm, ci, ds = [], {}, 0, sp + cbc
        for tag, vpe, mask, end_flag in tag_table:
            if end_flag == 1:
                ci += 1
                continue
            val = d[sp + ci] & mask
            if val == 0:
                continue
            if val == mask:
                if self._count_bits(mask) > 1:
                    used, val = self._varwidth(d, ds)
                    ds += used
                    tlist.append((tag, None, val, vpe))
                else:
                    tlist.append((tag, 1, None, vpe))
            else:
                while mask & 1 == 0:          # 去掉掩码低位的 0，值同步右移
                    mask >>= 1
                    val >>= 1
                tlist.append((tag, val, None, vpe))
        for tag, vc, vb, vpe in tlist:
            vals = []
            if vc is not None:
                for _ in range(vc):
                    for _ in range(vpe):
                        used, x = self._varwidth(d, ds)
                        ds += used
                        vals.append(x)
            else:
                tc = 0
                while tc < vb:
                    used, x = self._varwidth(d, ds)
                    ds += used
                    tc += used
                    vals.append(x)
            hm[tag] = vals
        return hm

    def get_index_data(self, idx):
        """返回 (条目列表, CTOC 文本字典)。

        条目为 [条目文字 bytes, 标签映射 dict]；CTOC 键为全局偏移
        （每跨一条 CTOC 记录，基准偏移增加 0x10000）。
        """
        u32 = lambda d, o: struct.unpack_from(">L", d, o)[0]
        outtbl, ctoc = [], {}
        d = self.load(idx)
        ih = self._parse_indx(d)
        cnt = ih["count"]
        rec_off = 0
        off = idx + cnt + 1
        for j in range(ih["nctoc"]):
            cd = self._read_ctoc(self.load(off + j))
            for k in cd:
                ctoc[k + rec_off] = cd[k]
            rec_off += 0x10000
        cbc, tag_table = self._read_tag_section(ih["len"], d, u32)
        for i in range(idx + 1, idx + 1 + cnt):
            ed = self.load(i)
            eh = self._parse_indx(ed)
            idxt = eh["start"]
            ec = eh["count"]
            positions = [struct.unpack_from(">H", ed, idxt + 4 + 2 * j)[0]
                         for j in range(ec)]
            positions.append(idxt)
            for j in range(ec):
                sp, ep2 = positions[j], positions[j + 1]
                tl = ed[sp]
                text = ed[sp + 1:sp + 1 + tl]
                tm = self._get_tag_map(
                    cbc, tag_table, ed, sp + 1 + tl, ep2)
                outtbl.append([text, tm])
        return outtbl, ctoc


# ---------------------------------------------------------------------------
# KF8 主类
# ---------------------------------------------------------------------------
class KF8Book:
    """加载并解析一本 KF8（azw3），提供正文分片、目录与图片按需读取。"""

    def __init__(self, path=None, data=None):
        if path is not None:
            with open(path, "rb") as f:
                data = f.read()
        if not data:
            raise KF8Error("文件为空")
        self.db = PalmDB(data)
        self._u32 = lambda b, o: struct.unpack_from(">L", b, o)[0]
        self._u16 = lambda b, o: struct.unpack_from(">H", b, o)[0]
        self.parts = []
        self.toc = []
        self.slots = []          # 资源槽：(记录号, 图片格式) 或 None
        self._parse()

    # -- 头部与解压 --------------------------------------------------------
    def _make_reader(self, r0):
        comp = self._u16(r0, 0x0)
        if comp == 0x4448:                       # Huffman（KF8 主流）
            reader = HuffcdicReader()
            huffoff, huffnum = self._u32(r0, 0x70), self._u32(r0, 0x74)
            reader.load_huff(self.db.load(huffoff))
            for k in range(1, huffnum):
                reader.load_cdic(self.db.load(huffoff + k))
        elif comp == 1:
            reader = UncompressedReader()
        elif comp == 2:
            reader = PalmdocReader()
        else:
            raise KF8Error("不支持的压缩方式 0x%X" % comp)
        return reader

    def _trim_info(self, r0):
        """根据 flags 计算尾部裁剪参数：multibyte 标志与 trailer 数量。"""
        header_len, version = self._u32(r0, 0x14), self._u32(r0, 0x68)
        multibyte, trailers = 0, 0
        if header_len >= 0xE4 and version >= 5:
            flags = self._u16(r0, 0xF2)
            multibyte = flags & 1
            f = flags
            while f > 1:
                if f & 2:
                    trailers += 1
                f >>= 1
        return multibyte, trailers

    @staticmethod
    def _trim(d, multibyte, trailers):
        """裁掉每条记录尾部的 trailer 与 multibyte 尾字节。"""
        for _ in range(trailers):
            num = 0
            for v in d[-4:]:
                if v & 0x80:
                    num = 0
                num = (num << 7) | (v & 0x7F)
            d = d[:-num]
        if multibyte:
            num = (d[-1] & 3) + 1
            d = d[:-num]
        return d

    # -- 碎片重组 ----------------------------------------------------------
    def _build_parts(self, r0, raw, index):
        raw_size = len(raw)

        # FDST：把 rawML 划分成若干 flow
        fdst_p, fdst_cnt = self._u32(r0, 0xC0), self._u32(r0, 0xC4)
        fdst_tbl = [0, raw_size]
        if fdst_cnt > 1:
            fd = self.db.load(fdst_p)
            nsecf = self._u32(fd, 8)
            fdst_tbl = list(struct.unpack_from(
                ">%dL" % (nsecf * 2), fd, 12))[::2]
            fdst_tbl.append(raw_size)

        # SKEL：骨架定义（位置、长度、包含碎片数）
        skel_out, _ = index.get_index_data(self._u32(r0, 0xFC))
        skel_tbl = []
        for fp, (text, tm) in enumerate(skel_out):
            skel_tbl.append([fp, text, tm[1][0], tm[6][0], tm[6][1]])

        # FRAG：碎片定义（插入位置、所属 flow、长度等）
        frag_out, frag_ctoc = index.get_index_data(self._u32(r0, 0xF8))
        frag_tbl = []
        for text, tm in frag_out:
            ctoc_off = tm[2][0]
            frag_tbl.append([int(text), frag_ctoc[ctoc_off], tm[3][0],
                             tm[4][0], tm[6][0], tm[6][1]])

        flows = [raw[fdst_tbl[j]:fdst_tbl[j + 1]]
                 for j in range(len(fdst_tbl) - 1)]
        text = flows[0]
        parts, fp = [], 0
        for _, _, frag_cnt, skel_pos, skel_len in skel_tbl:
            baseptr = skel_pos + skel_len
            skeleton = text[skel_pos:baseptr]
            for _ in range(frag_cnt):
                insertpos, _, _, _, _, length = frag_tbl[fp]
                slice_ = text[baseptr:baseptr + length]
                ip = insertpos - skel_pos       # 相对骨架的插入点
                skeleton = skeleton[:ip] + slice_ + skeleton[ip:]
                baseptr += length
                fp += 1
            parts.append(skeleton)
        return parts

    # -- 目录 --------------------------------------------------------------
    def _build_toc(self, r0, index):
        ncx_idx = self._u32(r0, 0xF4)
        out, ctoc = index.get_index_data(ncx_idx)
        toc = []
        for text, tm in out:
            level = tm[4][0] if 4 in tm else 0
            pos = tm[1][0] if 1 in tm else None
            title = text.decode("utf-8", "replace")      # 默认取条目 name
            # 关键：真实中文标题在 tag3(noffs) 指向的 CTOC 文本中
            if 3 in tm and tm[3][0] in ctoc:
                title = ctoc[tm[3][0]].decode("utf-8", "replace")
            toc.append({"title": title, "level": level, "pos": pos})
        return toc

    # -- 资源槽 ------------------------------------------------------------
    @staticmethod
    def _image_kind(d):
        if d[:2] == b"\xff\xd8":
            return "jpeg"
        if d[:4] == b"\x89PNG":
            return "png"
        if d[:3] == b"GIF":
            return "gif"
        if d[:4] == b"RIFF":
            return "webp"
        return None

    def _build_slots(self, r0):
        """按 firstresource 到末尾逐条记录建槽：图片存 (记录号,格式)，其余 None。"""
        first = self._u32(r0, 0x6C)
        slots = []
        for i in range(first, self.db.count):
            kind = self._image_kind(self.db.load(i))
            slots.append((i, kind) if kind else None)
        return slots

    # -- 主解析流程 --------------------------------------------------------
    def _parse(self):
        r0 = self.db.load(0)
        # record0 内偏移 0x10 为 4 字节 MOBI 标识（BOOKMOBI 是 Palm DB 库名）
        if r0[0x10:0x14] != b"MOBI":
            raise KF8Error("不是 MOBI/KF8 文件")
        if self._u16(r0, 0xC):
            raise KF8Error("加密的 azw3 不支持在线阅读")
        records = self._u16(r0, 0x8)

        reader = self._make_reader(r0)
        multibyte, trailers = self._trim_info(r0)
        chunks = []
        for i in range(1, records + 1):
            record = self._trim(self.db.load(i), multibyte, trailers)
            chunks.append(reader.unpack(record))
        raw = b"".join(chunks)

        index = MobiIndex(self.db.load)
        self.parts = self._build_parts(r0, raw, index)
        self.toc = self._build_toc(r0, index)
        self.slots = self._build_slots(r0)

    # -- 对外接口 ----------------------------------------------------------
    def get_image(self, num):
        """按 kindle:embed 编号（从 1 开始）返回 (图片字节, MIME)；非图片槽返回 None。"""
        if num < 1 or num > len(self.slots):
            return None
        slot = self.slots[num - 1]
        if slot is None:
            return None
        record_no, kind = slot
        return self.db.load(record_no), _IMAGE_MIME[kind]

    @property
    def image_count(self):
        """资源槽中图片的总数。"""
        return sum(1 for s in self.slots if s)
