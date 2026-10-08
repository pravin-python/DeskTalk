/*
 * unsupported.js - classic (non-module) script, loaded through <script nomodule> in index.html.
 * Only browsers without ES module support run it; it replaces the blank page with a notice.
 * Plain ES5 on purpose: this file must work in the browsers that cannot run main.js.
 */
(function () {
  var body = document.body;
  if (!body) return;
  while (body.firstChild) body.removeChild(body.firstChild);

  var box = document.createElement('div');
  box.className = 'unsupported';

  var title = document.createElement('h1');
  title.appendChild(document.createTextNode('Please update your browser'));

  var text = document.createElement('p');
  text.appendChild(document.createTextNode(
    'DeskTalk needs a recent browser: Chrome or Edge 108+, Firefox 101+, or Safari / iOS 15.4+.'
  ));

  var hint = document.createElement('p');
  hint.className = 'muted';
  hint.appendChild(document.createTextNode('Your browser does not support JavaScript modules.'));

  box.appendChild(title);
  box.appendChild(text);
  box.appendChild(hint);
  body.appendChild(box);
}());
