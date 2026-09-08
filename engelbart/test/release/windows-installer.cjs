// Validation only: retain Berkeley's real handoff tests, replacing their Node
// CLI invocation with the public PowerShell installer and released executable.
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {SimulatedMachine} = require(path.join(process.env.BERKELEY_ROOT,'e2e/fixtures/simulated-machine.js'));
function run(command,args,env,cwd) {
  return new Promise((resolve,reject)=>{
    const p=spawn(command,args,{env,cwd,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='';p.stdout.on('data',d=>stdout+=d);p.stderr.on('data',d=>stderr+=d);
    p.on('error',reject);p.on('close',code=>resolve({code,stdout,stderr}));
  });
}
SimulatedMachine.prototype.installWithArgs = async function(args) {
  this.env.ENGELBART_INSTALL_DIR=path.join(this.root,'released-bin');
  const candidate=process.env.ENGELBART_CANDIDATE_BINARY;
  const result=candidate
    ? await run(candidate,['install',...args],this.env,this.workspace)
    : await run('powershell',['-NoProfile','-ExecutionPolicy','Bypass','-File',process.env.ENGELBART_INSTALL_SCRIPT,...args],this.env,this.workspace);
  if(result.code!==0) throw Error(`Published installer failed: ${JSON.stringify(result)}`);
  const manifest=JSON.parse(fs.readFileSync(path.join(this.managed,'install.json'),'utf8'));
  this.env.HC_EXECUTABLE=path.join(manifest.runtime,'Scripts','hc.exe');
  const binary=candidate || path.join(this.env.ENGELBART_INSTALL_DIR,'engelbart.exe');
  const version=await run(binary,['install','--dry-run'],this.env,this.workspace);
  if(version.code || !version.stdout.includes('engelbart-cli '+process.env.EXPECTED_VERSION) || !version.stdout.includes('Verified bundled backend '+process.env.EXPECTED_VERSION)) throw Error(JSON.stringify(version));
  const python=path.join(manifest.runtime,'Scripts','python.exe');
  console.log('Native binary and embedded HC verified:',process.env.EXPECTED_VERSION);
  const checks=await run(python,[process.env.ENGELBART_INSTALLED_CHECK],this.env,this.workspace);
  console.log('Installed Windows check result:',JSON.stringify(checks));
  if(checks.code) throw Error(`Installed Windows checks failed: ${JSON.stringify(checks)}`);
  console.log('Published Windows artifact:',version.stdout.trim(),checks.stdout.trim());
  return result;
};
