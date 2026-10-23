"""Gate Shexli JSON, retaining one documented, ZIP-checked 0.2.1 limitation."""
import argparse
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys
import zipfile


def is_clean(report):
    if not isinstance(report, dict): return False
    summary = report.get('summary')
    if not isinstance(summary, dict): return False
    counts = summary.get('severity_counts')
    return (summary.get('status') == 'clean'
            and type(summary.get('finding_count')) is int
            and summary['finding_count'] == 0
            and report.get('findings') == []
            and isinstance(counts, dict)
            and all(type(value) is int and value == 0 for value in counts.values()))


def is_shell51_tool_limitation(report, package, analyzer_version):
    """Recognize only 0.2.1's stale release bound, checked against the real ZIP.

    GNOME has approved Shell 51 submissions with this same experimental finding:
    https://extensions.gnome.org/review/75368
    Preserve the raw report; never suppress this rule for other versions/findings.
    """
    if analyzer_version != '0.2.1' or not isinstance(report, dict): return False
    if report.get('spec_version') != '2026-04-02': return False
    summary = report.get('summary')
    if not isinstance(summary, dict): return False
    counts = summary.get('severity_counts')
    if (summary.get('status') != 'issues_found'
            or type(summary.get('finding_count')) is not int or summary['finding_count'] != 1
            or summary.get('input_path') != str(package)
            or not isinstance(counts, dict) or set(counts) != {'error'}
            or type(counts['error']) is not int or counts['error'] != 1): return False
    artifacts = report.get('artifacts')
    if not isinstance(artifacts, dict) or artifacts.get('target_versions') != [51]: return False
    findings = report.get('findings')
    if not isinstance(findings, list) or len(findings) != 1: return False
    finding = findings[0]
    if (not isinstance(finding, dict) or finding.get('rule_id') != 'EGO-M-004'
            or finding.get('severity') != 'error'
            or finding.get('evidence') != [{'path': f'{package}:metadata.json', 'line': None,
                                          'snippet': "shell-version=['51']"}]): return False
    try:
        with zipfile.ZipFile(package) as archive:
            if archive.namelist().count('metadata.json') != 1: return False
            if archive.getinfo('metadata.json').file_size > 65536: return False
            metadata = json.loads(archive.read('metadata.json'))
        return isinstance(metadata, dict) and metadata.get('shell-version') == ['51']
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile):
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--package', type=Path,
                        help='Check the documented 0.2.1 Shell 51 limitation against this actual ZIP.')
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        print(f'Shexli report unavailable or invalid: {error}', file=sys.stderr)
        raise SystemExit(1)
    if is_clean(report):
        print('Shexli: clean; no findings.')
    else:
        try: analyzer_version = version('shexli')
        except PackageNotFoundError: analyzer_version = None
        if args.package and is_shell51_tool_limitation(report, args.package, analyzer_version):
            print('::warning title=Shexli 0.2.1 limitation::EGO-M-004 rejects the verified GNOME 51 target. '
                  'Exact ZIP metadata checked; original finding retained in shexli-report.')
            raise SystemExit(0)
        print('Shexli blocked publication. Inspect the JSON report; all findings require resolution.', file=sys.stderr)
        raise SystemExit(1)
