"""Unit tests for the backend helpers in main.py (no network, Gemini is faked)."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
import prompts
from schemas import FrontendRequest, HistoryRecord, MathTutorResponse

VALID_TUTOR_JSON = json.dumps(dict(
    is_correct_so_far=True, status_message='ok', correct_steps=[], errors=[],
    faint_hint=None, hint_location_x=None, hint_location_y=None, current_latex='x'))


def fake_client(*outcomes):
    """Fake Gemini client; each outcome is an Exception to raise or a text to return."""
    calls = []

    def generate_content(model, contents, config):
        calls.append(model)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)), calls


class HelperTests(unittest.TestCase):
    def test_decode_image_accepts_data_url_and_bare_base64(self):
        self.assertEqual(main.decode_image('aW1hZ2U='), b'image')
        self.assertEqual(main.decode_image('data:image/png;base64,aW1hZ2U='), b'image')

    def test_decode_image_rejects_bad_padding_with_400(self):
        with self.assertRaises(HTTPException) as caught:
            main.decode_image('aW1hZ2U')
        self.assertEqual(caught.exception.status_code, 400)

    def test_clean_and_parse_json_handles_fences_and_noise(self):
        self.assertEqual(main.clean_and_parse_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(main.clean_and_parse_json('```\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(main.clean_and_parse_json('Sure! {"a": 1} hope that helps'), {'a': 1})
        with self.assertRaises(json.JSONDecodeError):
            main.clean_and_parse_json('not json')

    def test_tutor_prompt_variants(self):
        hint = main.build_tutor_prompt(FrontendRequest(image_data='x', action_type='get_hint', hint_focus='rule'))
        self.assertTrue(hint.startswith(prompts.HINT_PROMPT_PAGE))
        self.assertIn(prompts.HINT_FOCUS_GUIDANCE['rule'], hint)
        check = main.build_tutor_prompt(FrontendRequest(image_data='x', action_type='check_logic', is_selection=True))
        self.assertEqual(check, prompts.CHECK_PROMPT_SELECTION)

    def test_learner_profile_is_capped_before_entering_the_prompt(self):
        request = FrontendRequest(image_data='x', action_type='check_logic', learner_profile={'notes': 'z' * 5000})
        prompt = main.build_tutor_prompt(request)
        base = prompts.CHECK_PROMPT_PAGE + prompts.LEARNER_PROFILE_INSTRUCTION.format(profile='')
        self.assertEqual(len(prompt), len(base) + main.MAX_LEARNER_PROFILE_CHARS)

    def test_analytics_prompt_numbers_attempts_and_lists_errors(self):
        records = [HistoryRecord(timestamp='t1', is_correct=False, latex='x', summary='s', error_clues=['a', 'b']),
                   HistoryRecord(timestamp='t2', is_correct=True)]
        prompt = main.build_analytics_prompt(records)
        self.assertIn('Attempt #1 [t1]: Status: FLAGGED ERRORS', prompt)
        self.assertIn('Errors: a; b', prompt)
        self.assertIn('Attempt #2 [t2]: Status: PASSED (Correct)', prompt)
        self.assertIn('Errors: None', prompt)


class FallbackTests(unittest.TestCase):
    def call(self, client):
        with patch.object(main, 'get_client', return_value=client):
            return main.generate_structured(contents=['hi'], system_instruction='s', schema=MathTutorResponse,
                                            temperature=0.2, label='TEST', failure_message='All failed')

    def test_first_model_success_stops_early(self):
        client, calls = fake_client(VALID_TUTOR_JSON)
        self.assertTrue(self.call(client).is_correct_so_far)
        self.assertEqual(calls, main.ACTIVE_MODELS[:1])

    def test_falls_through_errors_and_bad_json_in_order(self):
        client, calls = fake_client(RuntimeError('quota'), 'not json', VALID_TUTOR_JSON)
        self.assertEqual(self.call(client).status_message, 'ok')
        self.assertEqual(calls, main.ACTIVE_MODELS)

    def test_all_models_failing_returns_500_with_last_error(self):
        client, calls = fake_client(RuntimeError('boom'))
        with self.assertRaises(HTTPException) as caught:
            self.call(client)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail, 'All failed: boom')
        self.assertEqual(calls, main.ACTIVE_MODELS)


class AnalyticsRouteTests(unittest.TestCase):
    def test_empty_history_returns_placeholder_without_calling_gemini(self):
        client, calls = fake_client(RuntimeError('should not be called'))
        with patch.object(main, 'get_client', return_value=client):
            response = TestClient(main.app).post('/analytics', json={'records': []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), main.empty_analytics_response().model_dump())
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
