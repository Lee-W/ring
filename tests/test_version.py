from importlib.metadata import version

import ring


def test_package_version_uses_distribution_name() -> None:
    assert ring.__distribution__ == "ring-cli"
    assert ring.__version__ == version("ring-cli")
