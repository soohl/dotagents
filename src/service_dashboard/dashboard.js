for (const label of document.querySelectorAll('[data-path]')) {
  label.textContent = new URL(label.dataset.path, window.location.origin).href;
}
document.querySelector('#gateway-address').textContent = window.location.host;
document.querySelector('#access-label').textContent = window.location.protocol === 'https:'
  ? 'Shared sign-in for all services' : 'Local access';
