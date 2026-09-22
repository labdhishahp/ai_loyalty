-- 0003_step_user_message.sql
--
-- A step may be preceded by a user turn: the question itself on step 0, or a
-- protocol reminder later ("you stopped without submitting findings").
--
-- Needed because the conversation is REBUILT from these rows on every advance.
-- A user turn that exists only in memory would vanish between requests, and the
-- model would be replayed a conversation that never happened -- the failure
-- being silent and the symptom being a model that appears to ignore instructions.
--
-- A separate migration rather than an edit to 0002, which is already applied.
-- Editing applied history makes environments diverge; the runner refuses it.

alter table ops.agent_steps add column user_message text null;
