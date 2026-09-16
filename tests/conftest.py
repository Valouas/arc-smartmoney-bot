"""Tests never load the user's .env (including a parent workspace .env)."""
import dotenv
import os
import tempfile

dotenv.load_dotenv = lambda *args, **kwargs: False

# Never read the user's reviewed DEX registry during deterministic/network tests.
# Infrastructure regressions inject their own fixture registry explicitly.
_registry_home = tempfile.TemporaryDirectory(prefix='arc-test-registry-')
os.environ['TRUSTED_DEX_REGISTRY'] = os.path.join(_registry_home.name, 'absent.json')
