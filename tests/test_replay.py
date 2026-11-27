"""Record / replay of provider calls (easyq_common.replay): record once, replay without calling, misses never call."""
import asyncio
import tempfile
import unittest

from easyq_common.replay import ProviderStore, RecordedEmbedder, RecordedLLM, RecordedSTT, ReplayMiss


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, model, system, prompt, max_tokens):
        self.calls += 1
        return {'text': 'ANSWER\n충돌 [1].', 'model': model, 'stop_reason': 'end_turn', 'input_tokens': 10, 'output_tokens': 5}

    def stream(self, model, system, prompt, max_tokens, on_text):
        self.calls += 1
        parts = ['ANSWER\n', '충돌은 ', '체이닝 [1].']
        for i, part in enumerate(parts):
            if on_text(part) is False:
                return {'text': ''.join(parts[:i + 1]), 'model': model, 'stop_reason': 'stopped', 'input_tokens': None, 'output_tokens': None}
        return {'text': ''.join(parts), 'model': model, 'stop_reason': 'end_turn', 'input_tokens': 12, 'output_tokens': 7}


class CountingEmbedder:
    def __init__(self):
        self.batches = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t))] + [0.0] * 767 for t in texts]

    embed_query = embed


class FakeLiveSTT:
    """Emits one final per 1 s of audio received."""
    def open(self, terms, on_final):
        outer = self

        class Stream:
            async def send(self, pcm, start_ms):
                await on_final(start_ms, start_ms + len(pcm) // 32, f'발화 {start_ms}')

            async def close(self):
                outer.closed = True
        return Stream()


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self, mode):
        return ProviderStore(self.tmp.name, mode)

    def test_llm_complete_records_once_then_replays_without_calling(self):
        live = CountingLLM()
        first = RecordedLLM(live, self.store('record')).complete('claude-sonnet-5-5', 'sys', '질문', 300)
        again = RecordedLLM(live, self.store('record')).complete('claude-sonnet-5-5', 'sys', '질문', 300)
        replayed = RecordedLLM(CountingLLM(), self.store('replay')).complete('claude-sonnet-5-5', 'sys', '질문', 300)
        self.assertEqual(live.calls, 1)   # the second record-mode call reused the stored answer
        self.assertEqual(first['text'], replayed['text'])
        self.assertEqual(again['output_tokens'], 5)

    def test_replay_miss_raises_and_never_calls_the_provider(self):
        live = CountingLLM()
        with self.assertRaises(ReplayMiss):
            RecordedLLM(live, self.store('replay')).complete('claude-sonnet-5-5', 'sys', '새 질문', 300)
        self.assertEqual(live.calls, 0)

    def test_llm_stream_replays_chunks_and_honours_early_stop(self):
        RecordedLLM(CountingLLM(), self.store('record')).stream('m', 's', 'p', 300, lambda chunk: None)
        seen = []
        result = RecordedLLM(CountingLLM(), self.store('replay')).stream('m', 's', 'p', 300, seen.append)
        self.assertEqual(seen, ['ANSWER\n', '충돌은 ', '체이닝 [1].'])
        self.assertEqual(result['text'], 'ANSWER\n충돌은 체이닝 [1].')
        stopped = RecordedLLM(CountingLLM(), self.store('replay')).stream('m', 's', 'p', 300, lambda chunk: False)
        self.assertEqual((stopped['text'], stopped['stop_reason']), ('ANSWER\n', 'stopped'))

    def test_early_stopped_stream_is_not_recorded(self):
        RecordedLLM(CountingLLM(), self.store('record')).stream('m', 's', 'p', 300, lambda chunk: False)
        with self.assertRaises(ReplayMiss):
            RecordedLLM(CountingLLM(), self.store('replay')).stream('m', 's', 'p', 300, lambda chunk: None)

    def test_embedder_calls_only_for_unrecorded_texts(self):
        live = CountingEmbedder()
        RecordedEmbedder(live, self.store('record'), 'gemini-embedding').embed(['해시', '충돌'])
        vectors = RecordedEmbedder(live, self.store('record'), 'gemini-embedding').embed(['충돌', '체이닝'])
        self.assertEqual(live.batches, [['해시', '충돌'], ['체이닝']])
        self.assertEqual(vectors[0][0], 2.0)
        with self.assertRaises(ReplayMiss):   # query vectors are a different task type
            RecordedEmbedder(CountingEmbedder(), self.store('replay'), 'gemini-embedding').embed_query(['해시'])

    def test_stt_records_finals_relative_to_stream_start_and_replays_them_in_time(self):
        async def run(stt, frames):
            got = []

            async def on_final(start, end, text):
                got.append((start, end, text))
            stream = stt.open(['해시'], on_final)
            for start_ms in frames:
                await stream.send(b'\0' * 32000, start_ms)   # 1 s of 16 kHz PCM16
            await stream.close()
            return got

        recorded = asyncio.run(run(RecordedSTT(FakeLiveSTT(), self.store('record'), 'lecture-1'), [5000, 6000]))
        self.assertEqual(recorded, [(5000, 6000, '발화 5000'), (6000, 7000, '발화 6000')])
        # Replay starts at a different lecture time: the same transcript, shifted to the new stream start.
        replayed = asyncio.run(run(RecordedSTT(None, self.store('replay'), 'lecture-1'), [0, 1000]))
        self.assertEqual(replayed, [(0, 1000, '발화 5000'), (1000, 2000, '발화 6000')])

    def test_live_mode_is_rejected_as_a_store(self):
        with self.assertRaises(ValueError):
            ProviderStore(self.tmp.name, 'sometimes')


if __name__ == '__main__':
    unittest.main()
