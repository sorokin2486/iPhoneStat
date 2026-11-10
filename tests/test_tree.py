from iphonestat.tree import (
    KIND_APP,
    KIND_APPDATA,
    KIND_BUNDLE,
    KIND_DIR,
    KIND_FREE,
    KIND_HIDDEN,
    KIND_IMAGE,
    KIND_OTHER,
    KIND_SYSTEM,
    KIND_VIDEO,
    DiskUsage,
    build_app_node,
    build_root,
    collapse_children,
    group,
    kind_for_filename,
    leaf,
    parse_disk_usage,
    to_dict,
)

GB = 1000**3


def test_group_sums_sizes_sorts_descending_and_drops_empty_children():
    node = group("dir", KIND_DIR, [leaf("a", 10, KIND_OTHER), leaf("empty", 0, KIND_OTHER), leaf("b", 30, KIND_OTHER)])

    assert node.size == 40
    assert [child.name for child in node.children] == ["b", "a"]


def test_leaf_clamps_negative_size_to_zero():
    assert leaf("x", -5, KIND_OTHER).size == 0


def test_kind_for_filename_is_case_insensitive_and_defaults_to_other():
    assert kind_for_filename("IMG_0001.HEIC") == KIND_IMAGE
    assert kind_for_filename("clip.mov") == KIND_VIDEO
    assert kind_for_filename("Photos.sqlite") == KIND_OTHER
    assert kind_for_filename("no_extension") == KIND_OTHER


def test_collapse_children_returns_all_when_under_limit():
    files = [leaf("a", 1, KIND_OTHER), leaf("b", 2, KIND_OTHER)]

    assert [n.name for n in collapse_children(files, keep=5)] == ["b", "a"]


def test_collapse_children_folds_smallest_into_one_node_preserving_total():
    files = [leaf(f"f{i}", i, KIND_IMAGE) for i in range(1, 11)]

    collapsed = collapse_children(files, keep=3)

    assert [n.name for n in collapsed[:3]] == ["f10", "f9", "f8"]
    assert collapsed[3].size == sum(range(1, 8))
    assert collapsed[3].kind == KIND_IMAGE
    assert sum(n.size for n in collapsed) == sum(n.size for n in files)


def test_parse_disk_usage_reads_capacity_system_and_free():
    usage = parse_disk_usage(
        {
            "TotalDiskCapacity": 128 * GB,
            "TotalSystemCapacity": 12 * GB,
            "TotalDataCapacity": 116 * GB,
            "AmountDataAvailable": 20 * GB,
            "TotalDataAvailable": 21 * GB,
        }
    )

    assert usage == DiskUsage(total=128 * GB, system=12 * GB, free=20 * GB)


def test_parse_disk_usage_derives_system_when_not_reported():
    usage = parse_disk_usage({"TotalDiskCapacity": 64 * GB, "TotalDataCapacity": 55 * GB, "TotalDataAvailable": 5 * GB})

    assert usage == DiskUsage(total=64 * GB, system=9 * GB, free=5 * GB)


def test_parse_disk_usage_returns_none_without_capacity():
    assert parse_disk_usage(None) is None
    assert parse_disk_usage({}) is None
    assert parse_disk_usage({"TotalDiskCapacity": "garbage"}) is None


def test_build_app_node_splits_program_and_data():
    node = build_app_node(
        {
            "CFBundleIdentifier": "com.example.app",
            "CFBundleDisplayName": "Example",
            "ApplicationType": "User",
            "StaticDiskUsage": 300,
            "DynamicDiskUsage": 700,
        }
    )

    assert node is not None
    assert (node.name, node.size, node.kind, node.note) == ("Example", 1000, KIND_BUNDLE, "com.example.app")
    assert [(c.name, c.size, c.kind) for c in node.children] == [
        ("Документы и данные", 700, KIND_APPDATA),
        ("Программа", 300, KIND_APP),
    ]


def test_build_app_node_ignores_static_size_of_system_apps():
    node = build_app_node(
        {"CFBundleIdentifier": "com.apple.mobilesafari", "ApplicationType": "System", "StaticDiskUsage": 500, "DynamicDiskUsage": 40}
    )

    assert node is not None
    assert node.size == 40


def test_build_app_node_returns_none_for_app_without_size_info():
    assert build_app_node({"CFBundleIdentifier": "com.example.empty"}) is None


def test_build_app_node_nests_documents_and_keeps_data_total():
    documents = group("Documents", KIND_DIR, [leaf("movie.mp4", 400, KIND_VIDEO)])

    node = build_app_node({"CFBundleName": "Player", "StaticDiskUsage": 100, "DynamicDiskUsage": 500}, documents)

    assert node is not None
    data = node.children[0]
    assert data.size == 500
    assert [(c.name, c.size) for c in data.children] == [("Файлы (Documents)", 400), ("Кэш, базы и прочие данные", 100)]


def test_build_root_computes_hidden_remainder_from_disk_usage():
    disk = DiskUsage(total=1000, system=100, free=200)
    apps = [leaf("App", 300, KIND_APP)]
    media = group("Медиа", KIND_DIR, [leaf("a.jpg", 150, KIND_IMAGE)])

    root = build_root("iPhone", disk, apps, [], media)

    sizes = {child.kind: child.size for child in root.children}
    assert root.size == 1000
    assert sizes[KIND_HIDDEN] == 1000 - 100 - 200 - 300 - 150
    assert sizes[KIND_SYSTEM] == 100
    assert sizes[KIND_FREE] == 200


def test_build_root_never_reports_negative_hidden_size():
    disk = DiskUsage(total=1000, system=100, free=800)

    root = build_root("iPhone", disk, [leaf("App", 500, KIND_APP)], [], None)

    assert all(child.kind != KIND_HIDDEN for child in root.children)


def test_build_root_without_disk_usage_contains_only_visible_parts():
    root = build_root("iPhone", None, [leaf("App", 500, KIND_APP)], [], None)

    assert root.size == 500
    assert [child.name for child in root.children] == ["Приложения"]


def test_to_dict_uses_compact_keys_and_omits_empty_fields():
    node = group("root", KIND_DIR, [leaf("a", 5, KIND_OTHER, note="hint")])

    assert to_dict(node) == {"n": "root", "s": 5, "k": KIND_DIR, "c": [{"n": "a", "s": 5, "k": KIND_OTHER, "t": "hint"}]}
