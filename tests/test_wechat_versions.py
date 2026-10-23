"""Version table integrity checks; no network, no WeChat, no fixtures."""
from myatry import wechat_versions


def test_table_loads_with_required_fields():
    assert wechat_versions.supported_versions(), "兼容表不能为空"
    for version, spec in wechat_versions.SUPPORTED.items():
        assert spec.wechat_version == version
        assert spec.client_dir.startswith("state/hook/wechat-")
        assert spec.wechatagent_runtime
        assert spec.integration == "WeChatAgent-1"
        assert spec.backend == "wechat-hook"


def test_spec_for_lookup_semantics():
    assert wechat_versions.spec_for(wechat_versions.supported_versions()[0]) is not None
    assert wechat_versions.spec_for("0.0.0.0") is None
    assert wechat_versions.spec_for(None) is None
    assert wechat_versions.spec_for(123) is None
