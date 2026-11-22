import json
import re
from pathlib import Path

import pytest

from test_agent_runner import repo, seed  # noqa: F401 (fixtures)
from aiep.container_policy import GATEWAY_ENTRYPOINT, GATEWAY_KEEPALIVE_ARGS, GatewayContext
from aiep.gateway_runner import GatewayImageUnpinned, gateway_context, load_gateway_image

REAL = Path(__file__).resolve().parents[1]
CFG = REAL / "config" / "gateway_image.json"
DIGEST = "sha256:" + "3" * 64
IMAGE_ID = "sha256:" + "4" * 64


# The Gate 1c evidence, written out literally: changing the pin must change this test in the same commit.
INDEX_DIGEST = "sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
ARM64_DIGEST = "sha256:c61cffc3bacb94ed026ff8b45d5114b7693dd0420baca0a1ace0e3d0ce9035fd"
PINNED_REF = "docker.io/library/python@" + ARM64_DIGEST
ORIGINAL_KEYS = {"schema", "status", "status_note", "image", "repo_digest", "image_id", "pin_rule", "python", "python_note", "uid", "gid",
                 "user_note", "entrypoint", "command", "entrypoint_note", "declared_volumes", "declared_volumes_note", "rejected",
                 "to_verify_after_approval"}


def _real():
    return json.loads(CFG.read_text())


def test_the_repository_pin_matches_the_gate_1c_evidence_exactly():
    cfg = _real()
    assert cfg["status"] == "verified" and cfg["image"] == "python:3.12-slim"  # the tag is documentation; the pin is the digest
    assert cfg["repo_digest"] == PINNED_REF and cfg["image_id"] == ARM64_DIGEST and cfg["python"] == "/usr/local/bin/python3"
    assert cfg["declared_volumes"] == []
    # with the containerd image store the local image ID is the platform manifest digest: the two must agree
    assert cfg["repo_digest"].split("@")[1] == cfg["image_id"]
    # the pin is the arm64 PLATFORM manifest, not the multi-platform index (which would pull whatever the daemon prefers)
    assert INDEX_DIGEST not in (cfg["repo_digest"], cfg["image_id"]) and INDEX_DIGEST in cfg["status_note"]
    for fact in ("linux/arm64/v8", "3.12.16", "Debian 13", "trixie", "2026-11-05T08:09:10Z", "nothing in the image has been executed"):
        assert fact in cfg["status_note"], fact


def test_the_config_adds_no_fields_records_the_identity_and_leaves_runtime_settings_untouched():
    cfg = _real()
    assert set(cfg) == ORIGINAL_KEYS and cfg["schema"] == 2
    assert cfg["uid"] == 10001 and cfg["gid"] == 10001  # the identity the project owner approved: recorded, never defaulted or guessed
    assert "RECORDED" in cfg["user_note"] and "UNRESOLVED" not in cfg["user_note"] and "10001:10001" in cfg["status_note"]
    assert cfg["entrypoint"] == GATEWAY_ENTRYPOINT == ["/bin/sleep"] and cfg["command"] == GATEWAY_KEEPALIVE_ARGS == ["2147483647"]
    assert "digest" in cfg["pin_rule"] and "tag alone is never accepted" in cfg["pin_rule"]
    from aiep.container_policy import GATEWAY_MEM, GATEWAY_NANO_CPUS, GATEWAY_PIDS, GATEWAY_TMPFS_RULES
    assert (GATEWAY_MEM, GATEWAY_PIDS, GATEWAY_NANO_CPUS) == ("256m", 128, 1_000_000_000) and set(GATEWAY_TMPFS_RULES) == {"/opt/aiep-gw", "/tmp"}


def test_localstack_is_rejected_not_recommended():
    cfg = _real()
    assert "localstack" not in cfg["image"] and "localstack" not in cfg["repo_digest"]
    assert [r["image"] for r in cfg["rejected"]][0] == "localstack/localstack:latest"
    assert "sha256:801a3dff" not in CFG.read_text()  # the old LocalStack image ID is no longer pinned anywhere


def test_the_real_gateway_config_loads_with_exactly_the_approved_identity():
    pin = load_gateway_image(CFG)
    assert (pin.uid, pin.gid) == (10001, 10001) and type(pin.uid) is int and type(pin.gid) is int
    assert pin.image == PINNED_REF and pin.image_id == ARM64_DIGEST and pin.python == "/usr/local/bin/python3" and pin.declared_volumes == ()
    ctx = gateway_context(pin, agent_network="aiep-agent-net", proxy_network="aiep-proxy-net")
    assert ctx.user == "10001:10001" and ctx.uid == 10001 and ctx.gid == 10001 and ctx.image_id == ARM64_DIGEST


def _real_with_identity(tmp_path, **over):
    """A copy of the real config with ``over`` applied (the real file already carries the approved identity)."""
    d = _real()
    d.update(over)
    p = tmp_path / "g.json"
    p.write_text(json.dumps(d))
    return p


def test_a_verbatim_copy_of_the_real_config_loads_and_carries_the_evidence_values(tmp_path):
    pin = load_gateway_image(_real_with_identity(tmp_path))
    assert pin.image == PINNED_REF and pin.image_id == ARM64_DIGEST and pin.python == "/usr/local/bin/python3"
    assert pin.declared_volumes == () and (pin.uid, pin.gid) == (10001, 10001)
    ctx = gateway_context(pin, agent_network="aiep-agent-net", proxy_network="aiep-proxy-net")
    assert ctx.image_id == ARM64_DIGEST and ctx.python == "/usr/local/bin/python3" and ctx.user == "10001:10001"


@pytest.mark.parametrize("over", [
    dict(repo_digest="docker.io/library/python:3.12-slim"),  # a tag is not a pin
    dict(repo_digest="python@" + ARM64_DIGEST.upper()), dict(repo_digest="python@sha256:c61cffc3"),
    dict(repo_digest=None), dict(image_id=None), dict(image_id="python:3.12-slim"), dict(image_id=ARM64_DIGEST[:-1]),
    dict(status="unpinned"), dict(status="proposed"), dict(entrypoint=["/usr/local/bin/python3"]), dict(command=["python3"]),
    dict(python=None), dict(python="python3"), dict(python="/tmp/python3"), dict(python="/usr/local/bin/../bin/python3"),
    dict(uid=None), dict(gid=None), dict(uid=0), dict(gid=0),
])
def test_wrong_or_incomplete_variants_of_the_real_pin_are_refused(tmp_path, over):
    with pytest.raises(GatewayImageUnpinned):
        load_gateway_image(_real_with_identity(tmp_path, **over))


def test_the_loader_checks_the_form_of_the_digest_not_which_digest_so_the_exact_pin_is_asserted_by_these_tests(tmp_path):
    other = "docker.io/library/python@sha256:" + "0" * 64
    pin = load_gateway_image(_real_with_identity(tmp_path, repo_digest=other))  # well formed: the loader cannot know it is the wrong one
    assert pin.image == other and pin.image != PINNED_REF
    # which is why the exact digest and ID are pinned in test_the_repository_pin_matches_the_gate_1c_evidence_exactly, and why a created
    # container's inspect must report Image == image_id at run time (verify_inspect fails closed on any other image)


def test_removing_any_required_key_from_the_real_pin_is_refused_not_defaulted(tmp_path):
    for key in ("repo_digest", "image_id", "python", "entrypoint", "command", "status", "uid", "gid"):
        d = _real()
        del d[key]
        p = tmp_path / f"{key}.json"
        p.write_text(json.dumps(d))
        with pytest.raises(GatewayImageUnpinned):
            load_gateway_image(p)


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
    ctx = gateway_context(pin, agent_network="a", proxy_network="p")
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


# ------------------------------------------------------------------ the approved identity: validation, consistency, separation
@pytest.mark.parametrize("uid,gid", [
    (None, 10001), (10001, None), (0, 10001), (10001, 0), (0, 0), (-1, 10001), (10001, -1), (-10001, -10001),
    (True, 10001), (10001, True), (False, 10001), ("10001", 10001), (10001, "10001"), (10001.0, 10001), (10001, 10001.5),
    ([10001], 10001), (10001, {"gid": 10001}), ("", ""), ("root", "root"),
])
def test_null_root_negative_and_malformed_identities_are_rejected(tmp_path, uid, gid):
    with pytest.raises(GatewayImageUnpinned, match="uid and gid"):
        load_gateway_image(_real_with_identity(tmp_path, uid=uid, gid=gid))


def _real_gateway_spec():
    from aiep.gateway_runner import build_gateway_component
    from aiep.lifecycle import GatewaySpec
    pin = load_gateway_image(CFG)
    ctx = gateway_context(pin, agent_network="aiep-agent-net", proxy_network="aiep-proxy-net", real_key="sk-ant-REAL-KEY-CANARY-0123456789",
                          agent_cidr="172.30.1.0/24")
    return GatewaySpec(run_id="r1", image=pin.image, ctx=ctx, run_token="per-run-token", budget_usd="0.50", code=build_gateway_component(REAL),
                       proxy_endpoint="aiep.proxy.r1:3128")


def test_the_real_identity_is_used_for_the_container_user_every_tmpfs_and_the_environment():
    from aiep.lifecycle import build_gateway_create_kwargs
    kw = build_gateway_create_kwargs(_real_gateway_spec())
    assert kw["user"] == "10001:10001" and kw["image"] == PINNED_REF and kw["entrypoint"] == ["/bin/sleep"] and kw["command"] == ["2147483647"]
    assert set(kw["tmpfs"]) == {"/opt/aiep-gw", "/tmp"}
    for path, opts in kw["tmpfs"].items():
        assert "uid=10001,gid=10001" in opts and "mode=0700" in opts and "nosuid" in opts and "nodev" in opts, path
    assert kw["environment"]["HOME"] == "/tmp" and kw["network"] == "aiep-agent-net"


def test_the_gateway_security_and_resource_policy_is_unchanged_by_the_identity():
    from aiep.lifecycle import build_gateway_create_kwargs
    kw = build_gateway_create_kwargs(_real_gateway_spec())
    assert kw["read_only"] is True and kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges"]
    assert kw["mem_limit"] == "256m" and kw["pids_limit"] == 128 and kw["nano_cpus"] == 1_000_000_000
    for forbidden in ("ports", "volumes", "privileged", "devices", "cap_add", "network_mode", "ipc_mode", "pid_mode"):
        assert forbidden not in kw
    assert kw["labels"]["aiep.role"] == "gateway"


def test_the_agent_identity_is_separate_and_unchanged(seed):
    from aiep.agent_env import load_agent_config
    from aiep.agent_runner import tmpfs_for
    from test_agent_runner import spec_for
    agent = load_agent_config(REAL / "config" / "agent_identity.json")
    assert (agent.identity.name, agent.identity.uid, agent.identity.gid, agent.identity.home) == ("nonroot", 1000, 1000, "/home/nonroot")
    gw = gateway_context(load_gateway_image(CFG), agent_network="a", proxy_network="p")
    assert gw.user == "10001:10001" and agent.identity.user == "1000:1000" and gw.user != agent.identity.user
    assert (gw.uid, gw.gid) != (agent.identity.uid, agent.identity.gid)
    agent_tmpfs = " ".join(tmpfs_for(spec_for(seed)).values())
    assert "uid=1000,gid=1000" in agent_tmpfs and "10001" not in agent_tmpfs  # the agent's mounts never carry the gateway's identity
    assert json.loads((REAL / "config" / "agent_identity.json").read_text())["status"] == "verified"


def test_the_whole_gateway_lifecycle_runs_with_the_real_pin_and_verifies_ownership_as_10001(tmp_path, seed):
    from test_lifecycle import build, release, run as lifecycle_run
    host, gspec, aspec = build(tmp_path, seed, gspec=_real_gateway_spec())
    res = lifecycle_run(host, gspec, aspec)
    release(host)
    assert res.gateway_error is None, res.gateway_error
    assert res.status == "completed" and res.agent.valid and res.gateway_removed
    (_, gw_kw), = [c for c in host.created if c[0] == "gateway"]
    assert gw_kw["user"] == "10001:10001" and gw_kw["image"] == PINNED_REF
    assert host.inspects[host.gw_c.id]["Image"] == ARM64_DIGEST and host.inspects[host.gw_c.id]["Config"]["User"] == "10001:10001"
    assert host.gw_fs.gateway_started == [["/usr/local/bin/python3", "-m", "aiep.gateway"]]
    assert {c["user"] for c in host.api.calls if c["container"] == host.gw_c.id} == {"10001:10001"}  # every exec in the gateway ran as it
    assert {c["user"] for c in host.api.calls if c["container"] == host.ag_c.id} == {"1000:1000"}


def test_a_wrong_owner_in_the_gateway_tree_is_still_caught_with_the_real_identity(tmp_path, seed):
    from test_lifecycle import build, release, run as lifecycle_run
    host, gspec, aspec = build(tmp_path, seed, gspec=_real_gateway_spec())
    host.gw_fs.owner = (10001, 0)  # the injected tree comes out owned by the wrong group
    res = lifecycle_run(host, gspec, aspec)
    release(host)
    assert res.status == "gateway_failed" and "owner differs" in res.gateway_error and res.agent is None and res.gateway_removed
