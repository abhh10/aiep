import json
import re
from pathlib import Path

import pytest

from aiep.container_policy import GATEWAY_ENTRYPOINT, GATEWAY_KEEPALIVE_ARGS, GatewayContext
from aiep.gateway_runner import GatewayImageUnpinned, gateway_context, load_gateway_image

REAL = Path(__file__).resolve().parents[1]
CFG = REAL / "config" / "gateway_image.json"
DIGEST = "sha256:" + "3" * 64
IMAGE_ID = "sha256:" + "4" * 64


def test_the_recorded_design_is_python_slim_unpinned_and_marks_everything_unverified():
    cfg = json.loads(CFG.read_text())
    assert cfg["image"] == "python:3.12-slim" and cfg["status"] == "unpinned"
    assert cfg["repo_digest"] is None and cfg["image_id"] is None  # nothing has been pulled or inspected
    assert "UNVERIFIED" in cfg["python_note"] and cfg["to_verify_after_approval"]
    # nothing about the image is invented: interpreter, uid and gid are unresolved, not guessed
    assert cfg["python"] is None and cfg["uid"] is None and cfg["gid"] is None
    assert "UNRESOLVED" in cfg["python_note"] and "UNRESOLVED" in cfg["user_note"]
    assert cfg["entrypoint"] == GATEWAY_ENTRYPOINT and cfg["command"] == GATEWAY_KEEPALIVE_ARGS
    assert "digest" in cfg["pin_rule"] and "tag alone is never accepted" in cfg["pin_rule"]


def test_localstack_is_rejected_not_recommended():
    cfg = json.loads(CFG.read_text())
    assert "localstack" not in cfg["image"] and cfg["image_id"] is None
    assert [r["image"] for r in cfg["rejected"]][0] == "localstack/localstack:latest"
    assert "sha256:801a3dff" not in CFG.read_text()  # the old LocalStack image ID is no longer pinned anywhere


def test_the_unpinned_config_cannot_be_loaded():
    with pytest.raises(GatewayImageUnpinned, match="unpinned"):
        load_gateway_image(CFG)


def _write(tmp_path, **over):
    d = dict(status="verified", image="python:3.12-slim", repo_digest="python@" + DIGEST, image_id=IMAGE_ID,
             python="/usr/local/bin/python3", uid=10001, gid=10001, entrypoint=GATEWAY_ENTRYPOINT, command=GATEWAY_KEEPALIVE_ARGS,
             declared_volumes=[])
    d.update(over)
    p = tmp_path / "g.json"
    p.write_text(json.dumps(d))
    return p


def test_a_digest_pinned_verified_config_loads(tmp_path):
    pin = load_gateway_image(_write(tmp_path))
    assert pin.image == "python@" + DIGEST and pin.image_id == IMAGE_ID and pin.python == "/usr/local/bin/python3"
    assert (pin.uid, pin.gid) == (10001, 10001)
    ctx = gateway_context(pin, agent_network="a", egress_network="e")
    assert ctx.image_id == IMAGE_ID and ctx.user == "10001:10001" and ctx.python == pin.python
    assert isinstance(ctx, GatewayContext)


@pytest.mark.parametrize("over", [
    dict(repo_digest="python:3.12-slim"),                       # a tag is not a pin
    dict(repo_digest="python@sha256:abc"),                      # not a full digest
    dict(repo_digest="python@" + DIGEST.upper()),
    dict(repo_digest=None), dict(image_id=None), dict(image_id="python:3.12-slim"),
    dict(status="unpinned"), dict(status="proposed"),
    dict(entrypoint=["/docker-entrypoint.sh"]), dict(command=["python3"]),
    dict(python=None), dict(python=""), dict(python="python3"),
    dict(uid=None), dict(gid=None), dict(uid=0), dict(gid=0), dict(uid=-1), dict(uid=True), dict(gid=True),
    dict(uid="10001"), dict(gid=1.0),
])
def test_anything_but_an_immutable_digest_and_id_with_the_fixed_entrypoint_is_refused(tmp_path, over):
    with pytest.raises(GatewayImageUnpinned):
        load_gateway_image(_write(tmp_path, **over))


def test_missing_identity_keys_are_refused_not_defaulted(tmp_path):
    for key in ("python", "uid", "gid"):
        p = _write(tmp_path)
        d = json.loads(p.read_text())
        del d[key]
        p.write_text(json.dumps(d))
        with pytest.raises(GatewayImageUnpinned):
            load_gateway_image(p)
