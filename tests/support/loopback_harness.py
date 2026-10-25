"""Run a tailmsg.py file with 127.0.0.0/8 treated as a tailnet range, for loopback-only tests.

Used through the documented ISHONI_PYTHON interpreter override: tests point it at a small shim that
sends `.../tailmsg.py ARGS` here and everything else to the real Python. Nothing in the plugin or in
tailmsg.py knows about this file.
"""
import importlib.util
import ipaddress
import sys

core_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("tailmsg", core_path)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.TAILNET_NETS.append(ipaddress.ip_network("127.0.0.0/8"))
sys.argv = [core_path] + sys.argv[2:]
sys.exit(core.main())
