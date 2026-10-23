"""A successful analyzer process must not hide findings or an invalid report."""
import importlib.util
import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shexli_report', ROOT / 'tools/check-shexli.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def report(status='clean', count=0, findings=None, severities=None):
    return {'summary': {'status': status, 'finding_count': count,
                        'severity_counts': severities or {}},
            'findings': [] if findings is None else findings}


class ShexliReportTests(unittest.TestCase):
    def test_only_a_clean_empty_report_passes(self):
        self.assertTrue(gate.is_clean(report()))
        self.assertTrue(gate.is_clean(report(severities={'warning': 0, 'error': 0})))

    def test_every_finding_blocks_publication(self):
        for severity in ('error', 'warning', 'manual_review', 'info'):
            with self.subTest(severity=severity):
                self.assertFalse(gate.is_clean(report('issues_found', 1,
                    [{'severity': severity}], {severity: 1})))

    def test_nonclean_or_unknown_status_blocks_even_without_findings(self):
        for status in ('error', 'manual_review', 'issues_found', 'future-status', None):
            with self.subTest(status=status): self.assertFalse(gate.is_clean(report(status)))

    def test_inconsistent_or_malformed_results_never_pass(self):
        for value in (None, [], {}, {'summary': None}, {'summary': {}, 'findings': []},
                      report(count=1), report(count=False), report(count='0'),
                      report(findings=[{'severity': 'warning'}]),
                      report(severities={'warning': 1}), report(severities={'error': False}),
                      report(findings={}), report(severities={'error': '0'})):
            with self.subTest(value=value): self.assertFalse(gate.is_clean(value))


class Shell51CompatibilityTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / 'extension.zip'
        self.write_metadata({'shell-version': ['51']})
        self.result = report('issues_found', 1, [{
            'rule_id': 'EGO-M-004', 'severity': 'error',
            'evidence': [{'path': f'{self.package}:metadata.json',
                          'line': None, 'snippet': "shell-version=['51']"}],
        }], {'error': 1})
        self.result['spec_version'] = '2026-04-02'
        self.result['summary']['input_path'] = str(self.package)
        self.result['artifacts'] = {'target_versions': [51]}

    def write_metadata(self, metadata):
        with zipfile.ZipFile(self.package, 'w') as archive:
            archive.writestr('metadata.json', json.dumps(metadata))

    def allowed(self, result=None, version='0.2.1'):
        return gate.is_shell51_tool_limitation(self.result if result is None else result,
                                              self.package, version)

    def test_exact_known_false_positive_is_allowed_without_modifying_report(self):
        original = copy.deepcopy(self.result)
        self.assertTrue(self.allowed())
        self.assertEqual(self.result, original)
        self.assertFalse(gate.is_clean(self.result))

    def test_only_the_pinned_analyzer_and_spec_qualify(self):
        for version in ('0.2.0', '0.2.2', None):
            with self.subTest(version=version): self.assertFalse(self.allowed(version=version))
        self.result['spec_version'] = 'future-spec'
        self.assertFalse(self.allowed())

    def test_other_findings_or_inconsistent_counts_still_block(self):
        for field, value in (('finding_count', 0), ('finding_count', True),
                             ('severity_counts', {'error': 1, 'warning': 1}), ('status', 'clean')):
            changed = copy.deepcopy(self.result)
            changed['summary'][field] = value
            with self.subTest(field=field): self.assertFalse(self.allowed(changed))
        for severity in ('warning', 'manual_review'):
            changed = copy.deepcopy(self.result)
            changed['findings'].append({'rule_id': 'EGO-L-003', 'severity': severity})
            changed['summary']['finding_count'] = 2
            changed['summary']['severity_counts'][severity] = 1
            self.assertFalse(self.allowed(changed))
        self.result['findings'][0]['rule_id'] = 'EGO-M-003'
        self.assertFalse(self.allowed())

    def test_only_exact_stable_51_in_actual_archive_qualifies(self):
        for versions in (['52'], ['51.beta'], ['50', '51'], [51], [], None):
            self.write_metadata({'shell-version': versions})
            with self.subTest(versions=versions): self.assertFalse(self.allowed())

    def test_changed_evidence_artifacts_or_package_do_not_qualify(self):
        for key, value in (('path', 'other.zip:metadata.json'), ('line', 1),
                           ('snippet', "shell-version=['52']")):
            changed = copy.deepcopy(self.result)
            changed['findings'][0]['evidence'][0][key] = value
            with self.subTest(key=key): self.assertFalse(self.allowed(changed))
        changed = copy.deepcopy(self.result)
        changed['artifacts']['target_versions'] = [52]
        self.assertFalse(self.allowed(changed))
        self.result['summary']['input_path'] = 'other.zip'
        self.assertFalse(self.allowed())

    def test_malformed_reports_and_missing_or_bad_archive_block(self):
        for value in (None, [], {}, {'summary': []}, {'summary': {}, 'findings': [None]}):
            with self.subTest(value=value):
                self.assertFalse(gate.is_shell51_tool_limitation(value, self.package, '0.2.1'))
        self.package.write_bytes(b'invalid zip')
        self.assertFalse(self.allowed())
        with zipfile.ZipFile(self.package, 'w') as archive:
            archive.writestr('metadata.json', '{broken')
        self.assertFalse(self.allowed())
        self.package.unlink()
        self.assertFalse(self.allowed())
