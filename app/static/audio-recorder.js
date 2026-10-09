// Microphone access starts only after a click. Draft audio uses the normal upload flow.
function bindAudioRecorder(form, onFile, locked){
  if(!globalThis.MediaRecorder || !globalThis.navigator?.mediaDevices?.getUserMedia)return null;
  const button=MoyaiUI.createElement('button', document);button.type='button';button.className='quiet record-button';
  const microphoneIcon='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></svg>';
  const cancel=MoyaiUI.createElement('button', document);cancel.type='button';cancel.className='quiet';cancel.textContent='Cancel recording';cancel.hidden=true;
  const status=document.createElement('span');status.className='recording-status';status.setAttribute('role','status');
  form.querySelector('.composer-toolbar').prepend(button,cancel,status);
  let recorder,stream,timer,waiting=false,disposed=false,cancelled=false,seconds=0,generation=0;
  const busy=()=>waiting || !!recorder;
  const release=()=>{clearInterval(timer);stream?.getTracks().forEach(track=>track.stop());stream=null;};
  const render=()=>{
    button.disabled=locked()||waiting;
    const label=recorder?'Stop recording':waiting?'Opening microphone…':'Record audio';
    button.className='quiet record-button'+(busy()?'':' is-idle');
    if(busy())button.textContent=label;else MoyaiUI.render(button, microphoneIcon);
    button.title=label;button.setAttribute('aria-label',label);cancel.hidden=!busy();
    status.textContent=recorder?`Recording ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} · 2 min max`:'';
  };
  const stop=(discard=false)=>{
    cancelled=discard;waiting=false;if(discard)generation++;
    if(recorder && recorder.state!=='inactive')recorder.stop();
    release();render();
  };
  button.onclick=async()=>{
    if(locked()||waiting)return;
    if(recorder){stop();return;}
    const attempt=++generation;waiting=true;cancelled=false;render();
    try{
      const acquired=await navigator.mediaDevices.getUserMedia({audio:true});
      if(disposed||cancelled||locked()||attempt!==generation){acquired.getTracks().forEach(track=>track.stop());return;}
      stream=acquired;
      const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(type=>MediaRecorder.isTypeSupported(type));
      if(!mime)throw Error('Recording is not supported in this browser. Upload an audio file instead.');
      recorder=new MediaRecorder(stream,{mimeType:mime,audioBitsPerSecond:64000});
      const chunks=[];let size=0;
      recorder.ondataavailable=event=>{
        if(event.data.size){chunks.push(event.data);size+=event.data.size;}
        if(size>10*1024*1024){toast('Recording reached the 10 MB limit. Try a shorter recording.');stop(true);}
      };
      recorder.onerror=()=>{toast('Recording failed. Try again or upload an audio file.');stop(true);};
      recorder.onstop=()=>{
        const type=recorder.mimeType;recorder=null;release();render();
        if(disposed||cancelled||!size)return;
        const extension=type.includes('mp4')?'m4a':type.includes('ogg')?'ogg':'webm';
        onFile(new File(chunks,'Voice message.'+extension,{type}));
      };
      seconds=0;recorder.start(1000);waiting=false;render();
      timer=setInterval(()=>{seconds++;render();if(seconds>=120)stop();},1000);
    }catch(error){
      if(attempt!==generation)return;
      release();recorder=null;
      if(!disposed&&!cancelled)toast(error.name==='NotAllowedError'?'Microphone permission was denied. Allow access or upload an audio file.':error.message||'Microphone unavailable. Upload an audio file instead.');
    }finally{if(attempt===generation){waiting=false;if(!disposed)render();}}
  };
  cancel.onclick=()=>stop(true);
  render();
  return {busy,render,cancel(){stop(true);},destroy(){disposed=true;stop(true);button.remove();cancel.remove();status.remove();}};
}
