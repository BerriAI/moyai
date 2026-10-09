const vm = require('node:vm');

// Controller tests use lightweight DOM doubles. Actual React/Radix behavior is
// covered by tests/browser/shadcn_ui.cjs against the production bundle.
function install(context) {
  context.MoyaiUI ||= {
    render: (host, html) => { host.innerHTML = html; },
    replace: (host, html) => { host.outerHTML = html; },
    insert: (host, position, html) => host.insertAdjacentHTML(position, html),
    createElement: (tag, doc = context.document) => doc.createElement(tag),
    createDialog: () => context.document.createElement('dialog'),
    isOpen: host => host.matches(':popover-open'),
  };
  return context;
}
module.exports = {
  ...vm,
  createContext: (context = {}, options) => vm.createContext(install(context), options),
  runInNewContext: (code, context = {}, options) => vm.runInNewContext(code, install(context), options),
};
install(globalThis);
