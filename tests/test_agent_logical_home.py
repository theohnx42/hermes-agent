"""Profile identity must survive externally stored/shared session databases."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import pytest

from hermes_constants import (
    get_agent_hermes_home, reset_hermes_home_override, set_hermes_home_override,
)
from tools.bot_mode_dm import _agent_home as bot_home
from tools.bot_mode_probe import _peers, _roster
from agent.system_prompt import _agent_home as prompt_home


class LogicalAgentHome(unittest.TestCase):
    def setUp(self):
        self.token = set_hermes_home_override(None)
        self.addCleanup(reset_hermes_home_override, self.token)

    @pytest.mark.platforms("posix")
    def test_symlink_storage_keeps_real_teammates_and_peers(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            logical, storage = base/'logical', base/'external'
            for root, names in [(logical, ['coder','intel','reviewer','ops']), (storage, ['coder'])]:
                root.mkdir()
                for name in names:
                    p=root/'profiles'/name
                    p.mkdir(parents=True)
                    (p/'profile.yaml').write_text('name: '+name+'\n')
            (storage/'state.db').touch()
            (logical/'state.db').symlink_to(storage/'state.db')
            (logical/'config.yaml').write_text('bot_peers:\n  nightfall: {}\n  hp1: {}\n')
            db=SimpleNamespace(db_path=(logical/'state.db').resolve())
            agent=SimpleNamespace(_session_db=db, _profile_home=logical)
            self.assertEqual([n for n,_ in _roster(db.db_path.parent)], ['default','coder'])
            self.assertEqual(_peers(db.db_path.parent), [])
            for home in [Path(bot_home(agent)),prompt_home(agent)]:
                self.assertEqual(home,logical)
                self.assertEqual([n for n,_ in _roster(home)], ['default','coder','intel','ops','reviewer'])
                self.assertEqual(_peers(home), ['hp1','nightfall'])

    def test_bound_profile_wins_shared_database_and_captured_launch_home(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            launch, reviewer = base/'launch', base/'profiles'/'reviewer'
            agent=SimpleNamespace(_profile_home=launch,
                                  _session_db=SimpleNamespace(db_path=base/'shared'/'state.db'))
            token=set_hermes_home_override(reviewer)
            try:
                self.assertEqual(bot_home(agent),str(reviewer))
                self.assertEqual(prompt_home(agent),reviewer)
            finally:
                reset_hermes_home_override(token)
            self.assertEqual(prompt_home(agent),launch)

    def test_named_profile_survives_lost_context_without_ambient_leak(self):
        import concurrent.futures
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            intel=base/'logical'/'profiles'/'intel'
            agent=SimpleNamespace(_profile_home=intel,
                                  _session_db=SimpleNamespace(db_path=base/'external'/'state.db'))
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(bot_home,agent).result(),str(intel))
                self.assertEqual(pool.submit(prompt_home,agent).result(),intel)

    def test_legacy_sessions_directory_precedes_physical_db(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            reviewer, external=base/'logical'/'profiles'/'reviewer', base/'external'
            agent=SimpleNamespace(logs_dir=reviewer/'sessions',
                                  _session_db=SimpleNamespace(db_path=external/'state.db'))
            self.assertEqual(get_agent_hermes_home(agent),reviewer)
            for logs in ['relative/sessions',base/'unrelated'/'logs',None]:
                agent.logs_dir=logs
                self.assertEqual(get_agent_hermes_home(agent),external)
            self.assertIsNone(get_agent_hermes_home(SimpleNamespace()))


if __name__ == '__main__':
    unittest.main()
