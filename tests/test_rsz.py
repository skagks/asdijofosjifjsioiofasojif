"""RSZ (.user/.pfb) repair against a small registry: CRC-only updates, rebuilding with an
older layout from the history, falling back to the game's own file, and refusing."""

import json
import struct

import pytest

from mhwmm.matfix import rsz

PARAM, CHILD, EXTRA = 0xA1, 0xB1, 0xC1
PARAM_NAME = "app.user_data.TestArmorParam"
OLD_CRC, NEW_CRC = 0x1111, 0x2222
NO_CHILD_CRC = 0x1110  # a version before _Child existed
CHILD_OLD, CHILD_CRC = 0x3330, 0x3333

PARAM_FIELDS = [["_Scale", "F32", 4, 4, 0], ["_HideHair", "Bool", 1, 1, 0], ["_NewFlag", "Bool", 1, 1, 0],
                ["_Mesh", "String", 4, 4, 0], ["_PartsOff", "S32", 4, 4, 1], ["_Child", "Object", 4, 4, 0]]
OLD_PARAM = [f for f in PARAM_FIELDS if f[0] != "_NewFlag"]
CHILD_FIELDS = [["_Value", "S32", 4, 4, 0]]


def registry(**extra) -> rsz.TypeRegistry:
    types = {format(PARAM, "x"): [format(NEW_CRC, "x"), PARAM_NAME, json.dumps(PARAM_FIELDS)],
             format(CHILD, "x"): [format(CHILD_CRC, "x"), "app.TestChild", json.dumps(CHILD_FIELDS)]}
    types.update(extra)
    return rsz.TypeRegistry({"types": types, "empty": {}, "alt": {},
                             "old": {format(PARAM, "x"): {format(OLD_CRC, "x"): json.dumps(OLD_PARAM),
                                                          format(NO_CHILD_CRC, "x"): json.dumps(PARAM_FIELDS[:-1])}}})


def s32(v):
    return struct.pack("<i", v)


def f32(v):
    return struct.pack("<f", v)


def text(s):
    s += "\0"
    return struct.pack("<I", len(s)) + s.encode("utf-16-le")


def encode(fields, values, base):
    out = bytearray()
    reg = registry()
    rsz.write_values(out, base, [tuple(f) for f in fields], values, rsz.Schema(), reg)
    return bytes(out)


def user_file(instances, resources=("Art/Model/armor.mesh",)) -> bytes:
    """A .user file: [(type id, crc, fields, values)] (the null instance is added first);
    fields None marks a reference to another .user file (no data of its own)."""
    strings = b"".join(r.encode("utf-16-le") + b"\0\0" for r in resources)
    header_size = 48
    res_table = header_size
    str_start = res_table + 8 * len(resources)
    rsz_off = (str_start + len(strings) + 15) // 16 * 16
    header = struct.pack("<4s3I3QQ", b"USR\0", len(resources), 0, 1, res_table, 0, rsz_off, 0)
    table = b""
    pos = str_start
    for r in resources:
        table += struct.pack("<Q", pos)
        pos += (len(r) + 1) * 2
    count = len(instances) + 1
    external = [i + 1 for i, inst in enumerate(instances) if inst[2] is None]
    inst_off = 48 + 16  # RSZ header, object table (one root, padded)
    user_off = (inst_off + count * 8 + 15) // 16 * 16
    data_off = (user_off + 16 * len(external) + 15) // 16 * 16
    block = bytearray(struct.pack("<4sIIIII", b"RSZ\0", 16, 1, count, len(external), 0))
    block += struct.pack("<QQQ", inst_off, data_off, user_off)
    block += struct.pack("<i", count - 1).ljust(16, b"\0")
    block += struct.pack("<II", 0, 0)
    for t, c, _f, _v in instances:
        block += struct.pack("<II", t, c)
    block = block.ljust(user_off, b"\0")
    for i in external:
        block += struct.pack("<iIQ", i, instances[i - 1][0], 0)
    block = block.ljust(data_off, b"\0")
    data = bytearray()
    for _t, _c, fields, values in instances:
        if fields is not None:
            data += encode(fields, values, rsz_off + data_off + len(data))
    return (header + table + strings).ljust(rsz_off, b"\0") + bytes(block) + bytes(data)


def param(fields, scale=1.0, hide=1, new=0, mesh="Art/Model/armor.mesh", parts=(3, 4), child=1):
    values = {"_Scale": f32(scale), "_HideHair": bytes([hide]), "_NewFlag": bytes([new]), "_Mesh": text(mesh),
              "_PartsOff": [s32(p) for p in parts], "_Child": s32(child)}
    return [values[f[0]] for f in fields]


def fields_of(data):
    info = rsz.parse(data)
    walked, _clean = rsz.walk_current(info, rsz.Schema(), registry())
    return {(i.index, f[0]): v for i in walked if i.fields for f, v in zip(i.fields, i.values)}, info


def game_file(**kw):
    return user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(7)]),
                      (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, scale=1.0, hide=0, new=1, **kw))])


def test_current_file_is_left_alone():
    plan = rsz.plan("a.user.3", game_file(), game_file(), registry())
    assert plan.state == "current" and plan.data is None


def test_only_the_crc_changed():
    mod = user_file([(CHILD, CHILD_OLD, CHILD_FIELDS, [s32(42)]),
                     (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, scale=2.5))])
    plan = rsz.plan("a.user.3", mod, game_file(), registry())
    assert (plan.state, plan.method) == ("fixable", "CRC 갱신")
    changed = [i for i, (a, b) in enumerate(zip(mod, plan.data)) if a != b]
    info = rsz.parse(mod)
    assert changed and all(info.inst_table + 8 + 4 <= i < info.inst_table + 8 + 8 for i in changed)  # only that CRC
    assert rsz.parse(plan.data).insts[1] == (CHILD, CHILD_CRC)


def test_old_layout_is_rebuilt_keeping_the_mods_values():
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]),
                     (PARAM, OLD_CRC, OLD_PARAM, param(OLD_PARAM, scale=2.5, hide=1, parts=(1, 2, 3), child=1))])
    plan = rsz.plan("a.user.3", mod, game_file(), registry())
    assert (plan.state, plan.method) == ("fixable", "재구성")
    values, info = fields_of(plan.data)
    assert info.insts[2] == (PARAM, NEW_CRC)
    assert values[(2, "_Scale")] == f32(2.5) and values[(2, "_HideHair")] == b"\x01"  # the mod's
    assert values[(2, "_PartsOff")] == [s32(1), s32(2), s32(3)] and values[(2, "_Child")] == s32(1)
    assert values[(2, "_NewFlag")] == b"\x01"  # the game's value for the new field
    assert values[(1, "_Value")] == s32(42)
    assert any("_NewFlag" in n for n in plan.notes)


def test_current_crcs_that_do_not_read_are_reported_not_changed():
    short = PARAM_FIELDS[:-1]  # every CRC is the game's, but the data has the old shape: the game misreads it
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, NEW_CRC, short, param(short, scale=3.0))])
    plan = rsz.plan("a.user.3", mod, game_file(), registry())
    assert plan.state == "stuck" and plan.data is None and "CRC는 현재 게임과 같지만" in plan.reason
    assert "app.user_data.TestArmorParam" in plan.detail


def test_fewer_fields_under_an_unknown_old_crc_are_not_guessed():
    short = PARAM_FIELDS[:-1]  # an older CRC the history does not know, missing the last field
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, 0x1999, short, param(short, scale=3.0))])
    game = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(7)]), (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, child=0))])
    plan = rsz.plan("a.user.3", mod, game, registry())
    # fields are often inserted mid-class, so "the current layout minus its last fields" is not
    # guessed (MHWmodfixer #22): the game file is used, without the mod's values
    assert (plan.state, plan.method) == ("fixable", "원본 그대로 교체")
    assert any("옮기지 못하고" in n for n in plan.notes)


def test_new_reference_falls_back_to_the_game_file_with_the_mods_values():
    short = PARAM_FIELDS[:-1]
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, NO_CHILD_CRC, short, param(short, scale=3.0))])
    plan = rsz.plan("a.user.3", mod, game_file(), registry())  # the game's _Child points at its own instance
    assert (plan.state, plan.method) == ("fixable", "원본 교체")
    values, _ = fields_of(plan.data)
    assert values[(2, "_Scale")] == f32(3.0) and values[(2, "_HideHair")] == b"\x01"  # the mod's values
    assert values[(2, "_Child")] == s32(1) and values[(1, "_Value")] == s32(42)
    assert any("옮겼습니다" in n for n in plan.notes)


def test_unknown_parts_are_not_replaced():
    extra = {format(EXTRA, "x"): ["4444", "app.CustomChain", json.dumps(CHILD_FIELDS)]}
    short = PARAM_FIELDS[:-1]
    mod = user_file([(EXTRA, 0x4444, CHILD_FIELDS, [s32(1)]), (CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]),
                     (PARAM, NO_CHILD_CRC, short, param(short))])
    plan = rsz.plan("a.user.3", mod, game_file(), registry(**extra))  # _Child would point at the game's own part
    assert plan.state == "stuck" and "app.CustomChain" in plan.reason and plan.data is None


def test_current_crcs_with_a_part_the_game_lacks_are_not_judged():
    extra = {format(EXTRA, "x"): ["4444", "app.CustomChain", json.dumps(CHILD_FIELDS)]}
    mod = user_file([(EXTRA, 0x4444, CHILD_FIELDS + CHILD_FIELDS, [s32(1), s32(2)]),  # registry is wrong for it
                     (CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS))])
    assert rsz.plan("a.user.3", mod, game_file(), registry(**extra)).state == "current"


def test_different_file_is_not_replaced():
    garbage = PARAM_FIELDS[:2]
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, 0x9999, garbage, param(garbage))],
                    resources=("Art/a.mesh", "Art/b.mesh", "Art/c.mesh", "Art/d.mesh"))
    plan = rsz.plan("a.user.3", mod, game_file(), registry())
    assert plan.state == "stuck" and "참조" in plan.reason


def test_resource_fields_are_strings():
    """A real MH Wilds .pfb reads exactly only with Resource fields as length-prefixed strings
    (REasy's reading, not MHWmodfixer's 4-byte one)."""
    fields = [["_Path", "Resource", 4, 4, 0], ["_Value", "S32", 4, 4, 0]]
    reg = registry(**{format(EXTRA, "x"): ["4444", "app.WithResource", json.dumps(fields)]})
    as_string = user_file([(EXTRA, 0x4444, [["_Path", "String", 4, 4, 0], ["_Value", "S32", 4, 4, 0]],
                            [text("Art/x.mesh"), s32(5)])])
    schema, _ = rsz.choose_schema(rsz.parse(as_string), rsz.parse(as_string), reg)
    assert schema.resource_string
    as_index = user_file([(EXTRA, 0x4444, [["_Path", "U32", 4, 4, 0], ["_Value", "S32", 4, 4, 0]], [s32(3), s32(5)])])
    assert rsz.choose_schema(rsz.parse(as_index), rsz.parse(as_index), reg) == (None, None)


def test_same_bytes_under_new_names_are_kept():
    """An older dump may describe the same bytes with other names ("v0" for "Enabled"): such an
    instance keeps its bytes, only the CRC changes."""
    old = [["v0", "Data", 1, 1, 0], ["v1", "Data", 4, 4, 0]]
    now = [["Enabled", "Bool", 1, 1, 0], ["_Value", "S32", 4, 4, 0]]
    reg = rsz.TypeRegistry({"types": {format(EXTRA, "x"): ["4444", "app.Streaming", json.dumps(now)]}, "empty": {},
                            "alt": {}, "old": {format(EXTRA, "x"): {"4440": json.dumps(old)}}})
    mod = user_file([(EXTRA, 0x4440, old, [b"\x01", s32(9)])])
    game = user_file([(EXTRA, 0x4444, now, [b"\x01", s32(1)])])
    plan = rsz.plan("a.pfb.18", mod, game, reg)
    assert (plan.state, plan.method) == ("fixable", "CRC 갱신")
    assert plan.data[rsz.parse(mod).data_start:] == mod[rsz.parse(mod).data_start:]  # every data byte kept


def test_bundled_registry_loads():
    reg = rsz.TypeRegistry.load()
    assert reg.meta.get("reasy_commit")
    armor = next(k for k, v in reg._types.items() if v[1] == "app.user_data.PlayerArmorVisualParam")
    fields = reg.fields(int(armor, 16))
    assert fields and fields[0][0] == "_BaseParam"
    assert reg._old.get(armor)  # older layouts of a type that changed over the game's updates


def test_non_rsz_file_is_ignored():
    assert rsz.plan("x.user.3", b"not rsz at all", None, registry()) is None
    assert rsz.plan("x.user.3", game_file(), None, registry()).state == "current"  # nothing to compare with


# ---- the real registry: PlayerArmorVisualParam (an armor's *_avp.user) ---------------------
def avp_files(mod_crc):
    """(mod, game) avp.user files: the mod written by an older game version with its own hair
    and scale settings, the game file current."""
    reg = rsz.TypeRegistry.load()
    by_name = {v[1]: int(k, 16) for k, v in reg._types.items()}
    root = by_name["app.user_data.PlayerArmorVisualParam"]
    color = by_name["app.user_data.PlayerArmorVisualParam.cArmorColorTypeSetting"]
    wp_on = by_name["app.user_data.PlayerArmorVisualParam.cArmorWpOnSetting"]
    torso = by_name["app.user_data.PlayerArmorVisualParam.cTorsoArmorSetting"]
    external = by_name["app.user_data.PlayerArmorVisualParam.ARMOR_CHILD_TYPE"]  # any type: no data of its own

    def values(fields, refs, custom):
        out = []
        for name, kind, size, _align, is_array in fields:
            if name in custom:
                out.append(custom[name])
            elif name in refs:
                ref = refs[name]
                out.append([s32(r) for r in ref] if is_array else s32(ref))
            elif is_array:
                out.append([])
            elif kind == "String":
                out.append(text(""))
            else:
                out.append(bytes(size))
        return out

    def build(layout, crc, with_wp_on, custom, parts):
        insts = [(external, 0, None, None), (external, 0, None, None)]
        insts.append((color, reg.crc(color), reg.fields(color), values(reg.fields(color), {}, {})))
        refs = {"_BaseParam": 1, "_ArmorVisualSetting": 2, "_ArmorColorTypeSettings": [3]}
        slots = ("Helm", "Body", "Arm", "Waist", "Leg")
        if with_wp_on:
            for slot in slots:
                refs[f"_ArmorWpOnSetting{slot}"] = len(insts) + 1
                insts.append((wp_on, reg.crc(wp_on), reg.fields(wp_on),
                              values(reg.fields(wp_on), {}, {"_IsEnableSetting": b"\x01"})))
        for slot in slots:
            refs[f"_TorsoArmorSetting{slot}"] = len(insts) + 1
            insts.append((torso, reg.crc(torso), reg.fields(torso),
                          values(reg.fields(torso), {"_ArmorPartsOffList": parts}, {})))
        insts.append((root, crc, layout, values(layout, refs, custom)))
        return user_file(insts, resources=("GameDesign/Equip/_Prefab/Armor/Female/004/000/004_000_base.user",))

    mine = {"_DispOffHear": b"\x01", "_HeadObjScale": f32(0.8)}
    old = reg.old_fields(root, mod_crc)
    mod = build(old, mod_crc, "_ArmorWpOnSettingHelm" in [f[0] for f in old], mine, [7, 8])
    game = build(reg.fields(root), reg.crc(root), True,
                 {"_HeadObjScale": f32(1.0), "_IsEffectCallCatchSlinger": b"\x01"}, [1])
    return mod, game, reg, root


def real_values(data, reg):
    info = rsz.parse(data)
    walked, _ = rsz.walk_current(info, rsz.Schema(), reg)
    return info, {(i.type_id, f[0]): v for i in walked if i.fields for f, v in zip(i.fields, i.values)}


def test_avp_from_before_the_weapon_settings_takes_the_game_file_with_the_mods_values():
    mod, game, reg, root = avp_files(0xD8684E78)  # its layout lacks five _ArmorWpOnSetting references
    plan = rsz.plan("004_000_avp.user.3", mod, game, reg)
    assert (plan.state, plan.method) == ("fixable", "원본 교체")
    info, values = real_values(plan.data, reg)
    assert info.insts[-1] == (root, reg.crc(root))
    assert values[(root, "_DispOffHear")] == b"\x01" and values[(root, "_HeadObjScale")] == f32(0.8)  # kept
    assert values[(root, "_ArmorWpOnSettingHelm")] != s32(0)  # the game's own part
    assert any("옮겼습니다" in n for n in plan.notes)


def test_avp_missing_one_flag_is_rebuilt():
    mod, game, reg, root = avp_files(0x394F5631)  # only _IsEffectCallCatchSlinger is new
    plan = rsz.plan("004_000_avp.user.3", mod, game, reg)
    assert (plan.state, plan.method) == ("fixable", "재구성")
    info, values = real_values(plan.data, reg)
    assert info.insts[-1] == (root, reg.crc(root))
    assert values[(root, "_DispOffHear")] == b"\x01" and values[(root, "_HeadObjScale")] == f32(0.8)
    assert any("_IsEffectCallCatchSlinger" in n for n in plan.notes)


def test_newer_registry_is_downloaded_and_preferred(tmp_path, monkeypatch):
    import io
    import lzma

    def blob(date):
        return lzma.compress(json.dumps({"meta": {"reasy_date": date}, "types": {"a1": ["1", "x", "[]"]}}).encode())

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rsz.TypeRegistry, "updates", tmp_path)
    assert rsz.download_registry(tmp_path, opener=lambda url: Response(blob("2000-01-01"))) is None  # older
    assert rsz.TypeRegistry.path() == rsz.REGISTRY_FILE
    assert rsz.download_registry(tmp_path, opener=lambda url: Response(blob("2099-01-01"))) == "2099-01-01"
    assert rsz.TypeRegistry.path() == tmp_path / rsz.REGISTRY_NAME
    with pytest.raises((lzma.LZMAError, ValueError, EOFError)):
        rsz.download_registry(tmp_path, opener=lambda url: Response(b"broken"))
    assert rsz.registry_date(tmp_path / rsz.REGISTRY_NAME) == "2099-01-01"  # a broken download changes nothing
    assert not list(tmp_path.glob("*.partial"))


def test_nothing_is_written_when_the_game_file_does_not_read():
    broken_game = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS + CHILD_FIELDS, [s32(7), s32(8)]),  # registry disagrees
                             (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS))])
    mod = user_file([(CHILD, CHILD_OLD, CHILD_FIELDS, [s32(42)]), (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS))])
    plan = rsz.plan("a.user.3", mod, broken_game, registry())
    assert plan.state == "stuck" and plan.data is None and plan.game_unreadable
    assert "형식 정보" in plan.reason and "읽기가 어긋남" in plan.detail


def test_another_sets_settings_file_is_kept_when_replacing():
    short = PARAM_FIELDS[:-1]
    mine = "GameDesign/Equip/_Prefab/Armor/Female/036/000/036_000_avp.user"
    theirs = "GameDesign/Equip/_Prefab/Armor/Female/041/001/041_001_avp.user"
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, NO_CHILD_CRC, short, param(short))],
                    resources=(mine,))
    game = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(7)]), (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS))],
                     resources=(theirs,))
    plan = rsz.plan("a.user.3", mod, game, registry())
    assert (plan.state, plan.method) == ("fixable", "원본 교체")
    strings = rsz.parse(plan.data).strings()
    assert mine in strings and theirs not in strings  # the borrowed reference stays (MHWmodfixer #30)


def test_chain_physics_settings_are_not_rewritten():
    chain = {format(PARAM, "x"): [format(NEW_CRC, "x"), "app.ChainSetting", json.dumps(PARAM_FIELDS)]}
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]),
                     (PARAM, OLD_CRC, OLD_PARAM, param(OLD_PARAM, scale=2.5))])
    plan = rsz.plan("a.pfb.18", mod, game_file(), registry(**chain))
    assert plan.method == "원본 교체" and any("체인" in n for n in plan.notes)


def test_unknown_old_crc_needs_clean_padding_for_a_crc_update():
    mod = bytearray(user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]),
                               (PARAM, 0x1999, PARAM_FIELDS, param(PARAM_FIELDS, scale=2.5))]))  # CRC not in the history
    info = rsz.parse(bytes(mod))
    walked, _clean = rsz.walk_current(info, rsz.Schema(), registry())
    mod[walked[2].start + 4 + 1 + 1] = 0x7F  # garbage where the current layout expects padding
    plan = rsz.plan("a.user.3", bytes(mod), game_file(), registry())
    assert plan.method != "CRC 갱신"


def test_unchanged_instances_keep_their_bytes_padding_included():
    first = bytearray(encode(PARAM_FIELDS, param(PARAM_FIELDS, child=0), 0))
    mod = bytearray(user_file([(PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, child=0)),
                               (PARAM, OLD_CRC, OLD_PARAM, param(OLD_PARAM, scale=2.5))]))
    info = rsz.parse(bytes(mod))
    pad = info.data_start + 4 + 1 + 1  # padding before _Mesh in the first (current) instance
    assert mod[pad] == 0 and len(first) > 8
    mod[pad] = 0x55  # the game's own writer sometimes leaves such bytes
    game = user_file([(PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, new=1, child=0)),
                      (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, new=1))])
    plan = rsz.plan("a.user.3", bytes(mod), game, registry())
    assert (plan.state, plan.method) == ("fixable", "재구성")
    assert plan.data[pad] == 0x55  # kept, not rewritten as zero


def test_object_references_must_point_at_earlier_instances():
    bad = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, child=2))])
    good = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]), (PARAM, NEW_CRC, PARAM_FIELDS, param(PARAM_FIELDS, child=1))])
    assert not rsz._verify(bad, rsz.Schema(), registry())  # points at itself
    assert rsz._verify(good, rsz.Schema(), registry())
    assert not rsz._verify(good, rsz.Schema(), registry(), {PARAM: 0x9999})  # a CRC that is not the current one


def test_replacing_refuses_when_the_mod_unhooked_a_part():
    short = PARAM_FIELDS[:-1]
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]),
                     (PARAM, NO_CHILD_CRC, short, param(short))])
    plain = rsz.plan("a.user.3", mod, game_file(), registry())
    assert plain.method == "원본 교체"  # no reference the mod has differs from the game's
    unhooked = [["_Base", "UserData", 4, 4, 0]] + PARAM_FIELDS
    reg = registry(**{format(PARAM, "x"): [format(NEW_CRC, "x"), PARAM_NAME, json.dumps(unhooked)]})
    reg._old[format(PARAM, "x")][format(NO_CHILD_CRC, "x")] = json.dumps(unhooked[:-1])
    ext = 0xD1
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(42)]),
                     (PARAM, NO_CHILD_CRC, unhooked[:-1], [s32(0)] + param(short))])  # _Base set to null on purpose
    game = user_file([(ext, 0x7777, None, None), (CHILD, CHILD_CRC, CHILD_FIELDS, [s32(7)]),
                      (PARAM, NEW_CRC, unhooked, [s32(1)] + param(PARAM_FIELDS, child=2))])
    plan = rsz.plan("a.user.3", mod, game, reg)
    assert plan.state == "stuck" and "연결" in plan.reason and "_Base" in plan.detail  # MHWmodfixer #30


def test_entries_inserted_mid_table_do_not_receive_other_entries_values():
    """An update inserted a skill in the middle of a table (and the table gained a reference, so
    it cannot simply be rewritten): pairing the n-th entries would slide the mod's values onto
    the wrong skills, so the game file is not filled in that way."""
    table = [["_Items", "Object", 4, 4, 1], ["_Default", "Object", 4, 4, 0]]
    old_table = [["_Items", "Object", 4, 4, 1]]
    reg = registry(**{format(EXTRA, "x"): ["5555", "app.SkillTable", json.dumps(table)]})
    reg._old[format(EXTRA, "x")] = {"5550": json.dumps(old_table)}
    mod = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(100)]), (CHILD, CHILD_CRC, CHILD_FIELDS, [s32(200)]),
                     (EXTRA, 0x5550, old_table, [[s32(1), s32(2)]])])
    game = user_file([(CHILD, CHILD_CRC, CHILD_FIELDS, [s32(10)]), (CHILD, CHILD_CRC, CHILD_FIELDS, [s32(15)]),
                      (CHILD, CHILD_CRC, CHILD_FIELDS, [s32(20)]),
                      (EXTRA, 0x5555, table, [[s32(1), s32(2), s32(3)], s32(2)])])
    plan = rsz.plan("skills.user.3", mod, game, reg)
    assert plan.state == "stuck" and plan.data is None and "연결" in plan.reason
