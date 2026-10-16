// Synthetic visual-preview data. The real API is tested separately on both databases.
module.exports = function performanceFixture(params, empty) {
  const hours = Number(params.get('hours') || 24), end = new Date(), start = new Date(end - hours * 3600000);
  const metric = (average, count=12) => ({count, total:count+2, average_ms:average, p50_ms:average*.8, p95_ms:average*2.2, max_ms:average*3, pending:1, incomplete:1, invalid:0});
  const missing = {count:0,total:0,average_ms:null,p50_ms:null,p95_ms:null,max_ms:null,pending:0,incomplete:0,invalid:0};
  const model = params.get('model') || '', kind = params.get('kind') || 'all', send_kind = params.get('send_kind') || 'all';
  const models = ['openai/gpt-6-astra','anthropic/claude-opus-4-6'].filter(name => !model || model === name);
  const blank = empty || !models.length;
  const startup = blank ? missing : metric(kind === 'follow_up' ? 1900 : 3400);
  const send = blank ? missing : metric(send_kind === 'explicit' ? 720 : 840);
  return {start:start.toISOString(),end:end.toISOString(),generated_at:end.toISOString(),hours,kind,model,send_kind,
    dispatch_coverage:{eligible:blank?0:14,legacy:0,complete_breakdowns:blank?0:12},
    computers:{cold_start:blank?missing:metric(5000,3),snapshot_restore:blank?missing:metric(2200,4),reuse:blank?missing:metric(80,5)},
    metrics:{model_dispatch:startup,dispatch_queue:blank?missing:metric(640),worker_to_broker:blank?missing:metric(1500),broker_prepare:blank?missing:metric(1260),computer_ready:blank?missing:metric(2200),first_model:startup,send_now:send,queue_pickup:blank?missing:metric(640),after_pickup:blank?missing:metric(2760)},
    models:blank?[]:models,by_model:blank?[]:models.map(model=>({model,startup,send_now:send})),
    trend:Array.from({length:24},(_,i)=>({start:new Date(+start+i*hours*150000).toISOString(),dispatch:blank||i%5===0?missing:metric(2600+i*80,3),send_now:blank||i%4===0?missing:metric(500+i*20,2)})),
    recent:blank?[]:models.map((model,i)=>({run_id:String(i+1).repeat(32),message_id:i+1,kind:'new_chat',model,harness:'hermes',status:i?'queued':'completed',received_at:end.toISOString(),send_now_at:end.toISOString(),send_now_kind:i?'automatic':'explicit',pickup_kind:'turn',dispatch_ms:i?null:3400,queue_ms:640,worker_to_broker_ms:1500,broker_prepare_ms:1260,computer_ready_ms:i?null:5000,computer_start_kind:'cold_start',model_request_id:'synthetic-request',first_model_ms:i?null:3400,send_now_ms:i?null:720})),
    truncated:{startup:false,send_now:false},sample_limit:10000};
};
