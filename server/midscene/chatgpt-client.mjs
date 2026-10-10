import OpenAI from 'openai';
import { AuthError } from './chatgpt-auth.mjs';

// Translate the SDK's Chat Completions contract to the authorized Responses
// contract. Only these fields are sent; unsupported provider options never leak.
export function toResponseInput(messages) {
  return messages.map(message => ({ role: message.role === 'system' ? 'developer' : message.role,
    content: typeof message.content === 'string' ? message.content : message.content.map(part => {
      if (part.type === 'text') return { type: message.role === 'assistant' ? 'output_text' : 'input_text', text: part.text };
      if (part.type === 'image_url') return { type: 'input_image', image_url: part.image_url.url, detail: part.image_url.detail ?? 'high' };
      throw new AuthError('unsupported_model_input');
    }) }));
}

export function createChatGPTClient(auth, { Client = OpenAI, signal, onError = () => {}, onMetrics = () => {} } = {}) {
  let sequence = 0;
  const complete = async (body, options = {}) => {
    const started = performance.now();
    const metrics = { sequence: ++sequence, model: body.model, ok: false };
    const elapsed = () => Math.round((performance.now() - started) * 1000) / 1000;
    try {
      const token = await auth.accessToken();
      metrics.auth_ms = elapsed();
      const client = new Client({ apiKey: token, baseURL: 'https://api.openai.com/v1',
        maxRetries: 0, timeout: 60000 });
      let completed;
      const outputItems = new Map();
      try {
        metrics.request_start_ms = elapsed();
        const stream = await client.responses.create({ model: body.model,
          input: toResponseInput(body.messages), store: false, stream: true }, { signal: signal && options.signal ? AbortSignal.any([signal, options.signal]) : signal ?? options.signal });
        metrics.headers_ms = elapsed();
        for await (const event of stream) {
          metrics.first_event_ms ??= elapsed();
          if (event.type === 'response.output_text.delta') {
            metrics.last_text_ms = elapsed();
            metrics.first_text_ms ??= metrics.last_text_ms;
          }
          if (event.type === 'response.completed') {
            metrics.completed_ms = elapsed();
            completed = event.response;
          }
          else if (event.type === 'response.output_item.done') outputItems.set(event.output_index, event.item);
          else if (['response.failed', 'response.incomplete', 'error'].includes(event.type))
            throw new AuthError('chatgpt_inference_failed');
        }
      } catch (error) {
        if (error instanceof AuthError) throw error;
        throw new AuthError(error.status === 401 ? 'chatgpt_sign_in_required' :
          error.status === 429 ? 'chatgpt_usage_limited' : 'chatgpt_inference_failed');
      }
      if (!completed || completed.status !== 'completed') throw new AuthError('chatgpt_incomplete_stream');
      // ChatGPT plan responses can omit output from the completion envelope.
      // Use finished stream items, but never accept them without response.completed.
      const output = completed.output?.length ? completed.output : [...outputItems.entries()].sort((a, b) => a[0] - b[0]).map(([, item]) => item);
      const content = output.filter(item => item.type === 'message')
        .flatMap(item => item.content ?? []).filter(part => part.type === 'output_text').map(part => part.text).join('\n');
      if (!content) throw new AuthError('chatgpt_empty_response');
      const usage = completed.usage && { prompt_tokens: completed.usage.input_tokens,
        completion_tokens: completed.usage.output_tokens, total_tokens: completed.usage.total_tokens };
      metrics.ok = true;
      if (completed.usage) {
        const counts = value => Object.fromEntries(Object.entries(value ?? {}).filter(([, n]) => typeof n === 'number'));
        metrics.usage = { ...counts(completed.usage),
          input_tokens_details: counts(completed.usage.input_tokens_details),
          output_tokens_details: counts(completed.usage.output_tokens_details) };
      }
      if (body.stream) return (async function* () {
        yield { model: completed.model, choices: [{ index: 0, delta: { content }, finish_reason: null }] };
        yield { model: completed.model, usage, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] };
      })();
      return { model: completed.model, usage, choices: [{ message: { role: 'assistant', content }, finish_reason: 'stop' }] };
    } finally {
      metrics.total_ms = elapsed();
      // Telemetry contains timings and counts only; never prompts, images or credentials.
      try { onMetrics(metrics); } catch { /* Diagnostics must not change execution. */ }
    }
  };
  return async () => ({ chat: { completions: { create: async (...args) => {
    try { return await complete(...args); }
    catch (error) { onError(error); throw error; }
  } } } });
}
