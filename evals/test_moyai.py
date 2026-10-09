"""Execute saved Lens cases against this checkout's real Python agent."""
import json
import os
from pathlib import Path
import subprocess
import sys

from evals.agent import AgentRunError, MoyaiAgent


def verification_for(input):
    suite = json.loads((Path(__file__).with_name('coding_cases.json')).read_text())
    matches = [case['meta']['verification_code'] for case in suite['cases'] if case['input'] == input]
    if len(matches) != 1:
        raise ValueError('Saved Lens input must exactly match one versioned coding case.')
    return matches[0]


def verify_solution(workspace, code):
    if not isinstance(code, str) or not code.strip():
        raise ValueError('The coding case needs independent verification code.')
    # Trusted, versioned test code owns verification. Lens supplies prompts,
    # not executable Python. Expected checks never enter the agent workspace.
    try:
        result = subprocess.run([sys.executable, '-I', '-c', code], cwd=workspace,
                                capture_output=True, text=True, timeout=15,
                                env={key: os.environ[key] for key in ('PATH', 'LANG') if key in os.environ})
    except subprocess.TimeoutExpired:
        return {'passed': False, 'detail': 'Independent verification timed out.'}
    return {'passed': result.returncode == 0,
            'detail': result.stdout[-2000:] if result.returncode == 0 else 'Independent verification failed.'}


def execution_metadata():
    from lens.config import Execution

    branch = os.environ.get('LENS_EVAL_BRANCH')
    if not branch:
        return None
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    role = os.environ.get('LENS_EVAL_ROLE', branch)
    pr = os.environ.get('LENS_EVAL_PR')
    return Execution(
        version=os.environ['LENS_VERSION'], branch=branch, pr=int(pr) if pr else None,
        ci_url=(f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{run_id}"
                if run_id and repository else ''),
        identity=(f"moyai-{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-{role}-{os.environ['LENS_VERSION']}"
                  if run_id else ''),
    )


def test_moyai(tmp_path):
    from lens import Lens

    lens = Lens(base_url=os.environ['LENS_BASE_URL'], api_key=os.environ['LENS_API_KEY'])
    with lens.evals.test(os.environ.get('MOYAI_EVAL_NAME', 'moyai-python-coding-regressions'),
                         execution=execution_metadata()) as evaluation:
        for index, case in enumerate(evaluation.cases):
            if case.followups:
                raise ValueError('The Python coding suite expects single-turn cases.')
            verification_code = verification_for(case.input)
            workspace = tmp_path / f'case-{index}'
            try:
                result = MoyaiAgent.from_env(workspace=workspace).run(input=case.input)
            except AgentRunError as exc:
                evaluation.record_error(case, exc)
                continue
            verification = verify_solution(workspace, verification_code)
            evaluation.record(case, output=json.dumps({'answer': result.output, 'verification': verification}),
                              trace_id=result.trace_id)
        report = evaluation.finish()
        artifact = os.environ.get('LENS_REPORT_PATH')
        if artifact:
            path = Path(artifact)
            path.parent.mkdir(parents=True, exist_ok=True)
            report.write_json(path)
        report.assert_passed()
