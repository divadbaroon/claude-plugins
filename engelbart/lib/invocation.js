'use strict';

// How this CLI tells the user to run it again. npm installs answer `npx
// engelbart-cli`; the compiled standalone binary sets `engelbart` at startup,
// because its users have no npm and an instruction they cannot run is worse
// than none. Read at message-render time, so even generated files (the
// credential helper, the env file) speak the channel they were written by.
let name = 'npx engelbart-cli';

module.exports = {
  invocation: () => name,
  setInvocation: (value) => { name = value; },
};
