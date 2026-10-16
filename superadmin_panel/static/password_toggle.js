// Adds a show/hide button to every password field on the page, so a user can
// check what they typed before submitting. The button sits inside the field's
// right edge and is type="button", so it never submits the form.
(function () {
    var EYE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
    var EYE_OFF = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.2M6.6 6.6C3.9 8.4 2 12 2 12s3.5 7 10 7a10.4 10.4 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';

    document.querySelectorAll('input[type="password"]').forEach(function (input) {
        var wrapper = document.createElement('span');
        wrapper.className = 'password-field';
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);

        var toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'password-toggle';
        wrapper.appendChild(toggle);

        function render(visible) {
            input.type = visible ? 'text' : 'password';
            toggle.innerHTML = visible ? EYE_OFF : EYE;
            toggle.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
            toggle.setAttribute('aria-pressed', visible ? 'true' : 'false');
            toggle.title = visible ? 'Hide password' : 'Show password';
        }

        toggle.addEventListener('click', function () {
            render(input.type === 'password');
            input.focus();
        });
        render(false);
    });
})();
