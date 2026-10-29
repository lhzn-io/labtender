from labtender import gpukinds


def test_kinds_and_bandwidth():
    assert gpukinds.kind("NVIDIA GeForce RTX 5080", False) == "discrete"
    assert gpukinds.kind("NVIDIA Thor", True) == "integrated"
    assert gpukinds.kind("Apple M4 Max", True) == "apple"
    assert gpukinds.bandwidth_gbs("NVIDIA GeForce RTX 5080") == 960
    assert gpukinds.bandwidth_gbs("NVIDIA GeForce RTX 5070 Ti") == 896
    assert (
        gpukinds.bandwidth_gbs("NVIDIA GeForce RTX 5080 Laptop GPU") == 896
    )  # not the desktop 960
    assert gpukinds.bandwidth_gbs("NVIDIA GeForce RTX 5070 Laptop GPU") is None
    assert gpukinds.bandwidth_gbs("NVIDIA A100-SXM4-80GB") == 2039
    assert (
        gpukinds.bandwidth_gbs("NVIDIA L40S") == 864 and gpukinds.bandwidth_gbs("NVIDIA L4") == 300
    )
    assert gpukinds.bandwidth_gbs("NVIDIA Jetson AGX Orin Developer Kit") == 204.8
    assert gpukinds.bandwidth_gbs(None) is None


def test_gauges_group_by_kind_and_rank_by_bandwidth(tmp_path):
    from fastapi.testclient import TestClient

    from labtender.db import Store
    from labtender.server import create_app

    store = Store(tmp_path / "t.sqlite")
    app = create_app(store=store, home=tmp_path, collect=False)
    gpus = {
        "a-mac": {"index": 0, "name": "Apple M4 Max", "unified": True, "mem_total_mib": 131072},
        "b-soc": {
            "index": 0,
            "uuid": "GPU-soc",
            "name": "NVIDIA Thor",
            "unified": True,
            "mem_total_mib": 125000,
        },
        "c-laptop": {
            "index": 0,
            "uuid": "GPU-lap",
            "name": "NVIDIA GeForce RTX 5080 Laptop GPU",
            "mem_total_mib": 16303,
        },
        "d-tower": {
            "index": 0,
            "uuid": "GPU-big",
            "name": "NVIDIA GeForce RTX 5080",
            "mem_total_mib": 16303,
        },
    }
    for host, g in gpus.items():
        store.add_host(host, actor="t")
        store.record_probe(host, {"gpus": [g], "gpu_procs": []}, [])
    r = TestClient(app).get("/api/gauges").json()
    assert [g["host"] for g in r["gpus"]] == ["d-tower", "c-laptop", "b-soc", "a-mac"]
    assert [g["kind"] for g in r["gpus"]] == ["discrete", "discrete", "integrated", "apple"]
    assert [g["kind"] for g in r["groups"]] == ["discrete", "integrated", "apple"]
    # A person gives the laptop part its figure; agents see the hold.
    store.set_card("GPU-lap", bandwidth_gbs=1000, hold="drives a display", actor="t")
    r = TestClient(app).get("/api/gauges").json()
    assert r["gpus"][0]["host"] == "c-laptop" and r["gpus"][0]["hold"] == "drives a display"
