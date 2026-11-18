import pytest

from aiep.stock_eval import harden_create_kwargs


def stock_kwargs():
    # shape of the kwargs swebench 5.0.2 passes to containers.create()
    return dict(
        name="sweb.eval.x.run",
        user="root",
        detach=True,
        cap_add=["SYS_ADMIN"],
    )


def test_strips_sys_admin_and_drops_all_caps():
    out = harden_create_kwargs(stock_kwargs())
    assert "cap_add" not in out
    assert out["cap_drop"] == ["ALL"]
    assert out["security_opt"] == ["no-new-privileges"]


def test_adds_resource_limits_and_keeps_other_kwargs():
    out = harden_create_kwargs(stock_kwargs())
    assert out["mem_limit"] == out["memswap_limit"] == "2g"
    assert out["pids_limit"] == 1024
    assert out["nano_cpus"] == 2_000_000_000
    assert out["user"] == "root" and out["detach"] is True


def test_does_not_mutate_input():
    kw = stock_kwargs()
    harden_create_kwargs(kw)
    assert kw["cap_add"] == ["SYS_ADMIN"]


@pytest.mark.parametrize(
    "bad",
    [
        {"privileged": True},
        {"volumes": {"/var/run/docker.sock": {"bind": "/x"}}},
        {"network_mode": "host"},
        {"pid_mode": "host"},
        {"cap_add": ["SYS_ADMIN", "NET_ADMIN"]},
    ],
)
def test_refuses_dangerous_options(bad):
    with pytest.raises(ValueError):
        harden_create_kwargs({**stock_kwargs(), **bad})
