import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  formatSlackMessage,
  extractTaskId,
  getColorForExitCode,
  type TaskFailureMessage,
  type SlackMessage,
} from './index';

// A PMF Engine failure as the EventBridge input transformer emits it: the
// experiment id is pulled from containerOverrides[0].environment[0].value.
function pmfFailure(
  overrides: Partial<TaskFailureMessage> = {}
): TaskFailureMessage {
  return {
    alarm: 'PMF Engine Task Failed',
    environment: 'dev',
    cluster: 'arn:aws:ecs:us-west-2:333022194791:cluster/pmf-engine-dev',
    taskArn:
      'arn:aws:ecs:us-west-2:333022194791:task/pmf-engine-dev/dad53d5d1234567890abcdef',
    experimentId: 'campaign-website-grader',
    stoppedReason: 'Essential container in task exited',
    exitCode: 1,
    time: '2026-06-22T20:29:08Z',
    logs: 'https://console.aws.amazon.com/cloudwatch/home',
    ...overrides,
  };
}

// Pull the flat {title: value} field map out of the Slack attachment so tests
// can assert on content without depending on field ordering.
function fieldMap(payload: SlackMessage): Record<string, string> {
  const fields = payload.attachments[0].fields ?? [];
  return Object.fromEntries(fields.map((f) => [f.title, f.value]));
}

test('surfaces the experiment id in the title and a field', () => {
  const payload = formatSlackMessage(pmfFailure());

  assert.equal(
    payload.attachments[0].title,
    ':x: PMF Engine Task Failed — campaign-website-grader'
  );
  assert.equal(fieldMap(payload).Experiment, 'campaign-website-grader');
});

test('keeps the existing fields alongside the new Experiment field', () => {
  const fields = fieldMap(formatSlackMessage(pmfFailure()));

  assert.equal(fields.Environment, 'dev');
  assert.equal(fields['Task ID'], 'dad53d5d');
  assert.equal(fields['Exit Code'], '1');
  assert.equal(fields['Stopped Reason'], 'Essential container in task exited');
});

test('omits the Experiment field when no experiment id is present', () => {
  // Other producers share this SNS topic and send no experimentId.
  const payload = formatSlackMessage(pmfFailure({ experimentId: undefined }));

  assert.equal(payload.attachments[0].title, ':x: PMF Engine Task Failed');
  assert.equal('Experiment' in fieldMap(payload), false);
});

test('treats empty, whitespace, or the literal "null" as absent', () => {
  // EventBridge substitutes a placeholder when the env path does not resolve:
  // "" in the normal case, the literal string "null" for an unmatched path.
  for (const experimentId of ['', '   ', 'null']) {
    const payload = formatSlackMessage(pmfFailure({ experimentId }));
    assert.equal(payload.attachments[0].title, ':x: PMF Engine Task Failed');
    assert.equal('Experiment' in fieldMap(payload), false);
  }
});

test('falls back to a generic title when alarm is missing', () => {
  const payload = formatSlackMessage(
    pmfFailure({ alarm: '', experimentId: undefined })
  );
  assert.equal(payload.attachments[0].title, ':x: ECS Task Failed');
});

test('combines the alarm fallback with an experiment suffix', () => {
  const payload = formatSlackMessage(
    pmfFailure({ alarm: '', experimentId: 'voter-turnout-model' })
  );
  assert.equal(
    payload.attachments[0].title,
    ':x: ECS Task Failed — voter-turnout-model'
  );
});

test('color reflects the exit code', () => {
  assert.equal(getColorForExitCode(1), '#FF0000');
  assert.equal(getColorForExitCode(137), '#FF6600'); // SIGKILL / OOM
  assert.equal(getColorForExitCode(143), '#FF9900'); // SIGTERM
  assert.equal(getColorForExitCode(99), '#CC0000'); // fallback
  assert.equal(
    formatSlackMessage(pmfFailure({ exitCode: 137 })).attachments[0].color,
    '#FF6600'
  );
});

test('extractTaskId returns the first 8 chars of the task uuid', () => {
  assert.equal(
    extractTaskId(
      'arn:aws:ecs:us-west-2:333022194791:task/pmf-engine-dev/dad53d5d1234567890abcdef'
    ),
    'dad53d5d'
  );
});

test('extractTaskId returns a short id unchanged rather than padding', () => {
  assert.equal(
    extractTaskId('arn:aws:ecs:us-west-2:1:task/cluster/abc12'),
    'abc12'
  );
});
