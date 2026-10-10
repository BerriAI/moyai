/* Logos are copied from the LiteLLM dashboard (ui/litellm-dashboard/public/assets/logos). */
(function(root){
  const logos={
    openai:'openai',azure:'microsoft_azure',azure_ai:'microsoft_azure',anthropic:'anthropic',
    gemini:'google',vertex_ai:'google',google:'google',bedrock:'bedrock',
    fireworks_ai:'fireworks',mistral:'mistral',xai:'xai',deepseek:'deepseek',groq:'groq',
    meta_llama:'meta_llama',moonshot:'moonshot',openrouter:'openrouter',together_ai:'togetherai',
    cohere:'cohere',minimax:'minimax',
  };
  const harnesses={'claude-agent-sdk':'claude-code',codex:'codex',pi:'pi',opencode:'opencode',hermes:'hermes'};
  function src(model){const file=logos[String(model||'').split('/')[0].toLowerCase()];return file?`/static/provider-logos/${file}.svg`:null;}
  function harness(id){const file=harnesses[id];return file?`/static/harness-logos/${file}.${file==='hermes'?'png':'svg'}`:null;}
  function sync(img,model,resolve=src){const url=resolve(model);img.hidden=!url;if(url)img.src=url;else img.removeAttribute('src');}
  const api={src,harness,sync};root.MoyaiProviderLogos=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
