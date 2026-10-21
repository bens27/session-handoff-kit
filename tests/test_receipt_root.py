"""Receipt confirmation through a differently spelled project root."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import chdir
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_protocol as protocol
import restart_preflight as restart


class ReceiptRootTests(unittest.TestCase):
    def test_receipt_keys_are_canonical_absolute_across_symlinked_roots(self):
        self.check_receipt(changed=False)

    def test_changed_real_dependency_is_refused_despite_matching_foreign_cwd_file(self):
        self.check_receipt(changed=True)

    def check_receipt(self, changed):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, foreign, home = base / 'project', base / 'foreign', base / 'home'
            for folder in (root, foreign, home):
                folder.mkdir()
            (root / '.handoffs').mkdir()
            alias = base / 'project-alias'
            alias.symlink_to(root, target_is_directory=True)
            original = 'first\nrequired excerpt\nlast\n'
            dependency = root / 'required.txt'
            dependency.write_text(original)
            (foreign / 'required.txt').write_text(original)
            with patch.dict(os.environ, {'HOME': str(home), 'USERPROFILE': str(home),
                                         'HANDOFF_TERMINAL_ID': 'fixture-terminal',
                                         'AGENTSROOM_AGENT_ID': ''}), chdir(foreign):
                saved = protocol.save(str(root), 'old', 'save-root', {
                    'topic': 'root-references', 'description': 'Use checkpoint dependencies',
                    'body': '## Objective\nContinue safely\n## Current state\nSaved\n## Next steps\nCheck dependency\n',
                    'references': ['required.txt#L2-L2']})
                self.assertEqual(saved['outcome'], 'saved', saved)
                ready = restart.preflight(str(root), saved['path'], 'old', 'fixture-terminal',
                                          'restart-root', workers=[], host_capable=True)
                self.assertEqual(ready['outcome'], 'ready', ready)
                prepared = protocol.prepare(saved['path'], str(alias), 'new')
                self.assertEqual(prepared['outcome'], 'retrieved', prepared)
                if changed:
                    dependency.write_text('first\nchanged real dependency\nlast\n')
                    with self.assertRaisesRegex(ValueError, 'Required dependency changed'):
                        restart.confirm_successor(str(alias), 'restart-root', 'new',
                                                  delivery_receipt=prepared['delivery_receipt'])
                    self.assertEqual(restart.status(str(root), 'restart-root')['state'], 'pending')
                else:
                    receipt = restart.confirm_successor(str(alias), 'restart-root', 'new',
                                                        delivery_receipt=prepared['delivery_receipt'])
                    self.assertEqual(receipt['state'], 'retrieved')
                    self.assertEqual(list(receipt['retrieval_receipt']['references']),
                                     [str(dependency) + '#L2-L2'])
                    self.assertFalse(receipt['host_confirmed'])
                self.assertNotIn('resumed_by:', Path(saved['path']).read_text())
