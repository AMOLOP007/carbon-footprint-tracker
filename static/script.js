// script.js - basic client side stuff for aetherra

// login form check
function validateLogin() {
    var email = document.getElementById('email').value;
    var pass = document.getElementById('password').value;

    if (!email || !pass) {
        alert('Please fill in all fields.');
        return false;
    }

    // basic email check
    if (!email.includes('@')) {
        alert('Please enter a valid email.');
        return false;
    }

    return true;
}

// register form check
function validateRegister() {
    var name = document.getElementById('name').value;
    var email = document.getElementById('email').value;
    var pass = document.getElementById('password').value;

    if (!name || !email || !pass) {
        alert('Please fill in all fields.');
        return false;
    }

    if (!email.includes('@')) {
        alert('Please enter a valid email.');
        return false;
    }

    // minimum password length
    if (pass.length < 4) {
        alert('Password must be at least 4 characters.');
        return false;
    }

    return true;
}

// hide flash messages after a few seconds
document.addEventListener('DOMContentLoaded', function () {
    var msgs = document.querySelectorAll('.flash-msg');
    msgs.forEach(function (msg) {
        setTimeout(function () {
            msg.style.opacity = '0';
            msg.style.transition = 'opacity 0.5s';
            setTimeout(function () { msg.remove(); }, 500);
        }, 4000);
    });

    // Initialize Theme Switcher
    initTheme();
});

// ---- THEME SWITCHER LOGIC ----

function initTheme() {
    const saved = localStorage.getItem('theme') || 'system';
    applyTheme(saved);

    // Inject Floating Controls
    const switcherHTML = `
        <div id="theme-switcher" style="position:fixed; bottom:20px; right:20px; z-index:9999; background:rgba(20,25,32,0.6); backdrop-filter:blur(10px); border:1px solid rgba(255,255,255,0.05); border-radius:30px; display:flex; gap:5px; padding:6px; box-shadow:0 5px 15px rgba(0,0,0,0.3); align-items: center;">
            <button onclick="setTheme('light')" class="theme-btn" id="btn-light" style="background:transparent; border:none; border-radius:50%; padding:8px; cursor:pointer; font-size:1.1rem; color: #fff; transition:0.3s;" title="Light Mode">☀️</button>
            <button onclick="setTheme('dark')" class="theme-btn" id="btn-dark" style="background:transparent; border:none; border-radius:50%; padding:8px; cursor:pointer; font-size:1.1rem; color: #fff; transition:0.3s;" title="Dark Mode">🌙</button>
            <button onclick="setTheme('system')" class="theme-btn" id="btn-system" style="background:transparent; border:none; border-radius:50%; padding:8px; cursor:pointer; font-size:1.1rem; color: #fff; transition:0.3s;" title="System Auto">💻</button>
        </div>
    `;
    if(!document.getElementById('theme-switcher')) {
        document.body.insertAdjacentHTML('beforeend', switcherHTML);
    }
    updateActiveButton(saved);
}

function setTheme(mode) {
    localStorage.setItem('theme', mode);
    applyTheme(mode);
    updateActiveButton(mode);
}

function applyTheme(mode) {
    let isLight = false;
    if (mode === 'system') {
        const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
        isLight = prefersLight;
    } else {
        isLight = (mode === 'light');
    }

    if (isLight) {
        document.documentElement.setAttribute('data-theme', 'light');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }

    // Update Flatpickr dynamically if it exists on page
    const fpTheme = document.getElementById('flatpickr-theme');
    if (fpTheme) {
        fpTheme.href = isLight ? "https://npmcdn.com/flatpickr/dist/themes/light.css" : "https://npmcdn.com/flatpickr/dist/themes/dark.css";
    }
}

function updateActiveButton(mode) {
    const btns = document.querySelectorAll('.theme-btn');
    if(!btns.length) return;
    btns.forEach(b => b.style.background = 'transparent');
    const active = document.getElementById('btn-' + mode);
    if(active) active.style.background = 'rgba(128,128,128,0.3)';
}

// listen for system OS changes in realtime
window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', e => {
    if(localStorage.getItem('theme') === 'system') {
        applyTheme('system');
    }
});
