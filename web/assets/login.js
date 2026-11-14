/* 登录 / 注册：图片验证码、密码强度、前端预校验 */
(function () {
  if (API.token()) location.href = "/";

  const tabs = document.querySelectorAll(".tab");
  const loginForm = document.getElementById("loginForm");
  const registerForm = document.getElementById("registerForm");
  const errorBox = document.getElementById("formError");

  const loginPhone = document.getElementById("loginPhone");
  const loginPwd = document.getElementById("loginPwd");
  const loginCode = document.getElementById("loginCode");
  const loginCaptchaImg = document.getElementById("loginCaptchaImg");
  const loginBtn = document.getElementById("loginBtn");

  const regPhone = document.getElementById("regPhone");
  const regName = document.getElementById("regName");
  const regPwd = document.getElementById("regPwd");
  const regConfirm = document.getElementById("regConfirm");
  const regCode = document.getElementById("regCode");
  const regCaptchaImg = document.getElementById("regCaptchaImg");
  const regBtn = document.getElementById("registerBtn");
  const regRoles = document.querySelectorAll('input[name="regRole"]');
  const inviteField = document.getElementById("inviteField");
  const regInvite = document.getElementById("regInvite");
  const meter = document.getElementById("pwdMeter");
  const pwdHint = document.getElementById("pwdHint");

  let captcha = { login: "", register: "" };

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.add("show");
  }
  function clearError() { errorBox.classList.remove("show"); }

  function switchTab(name) {
    clearError();
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    loginForm.classList.toggle("hidden", name !== "login");
    registerForm.classList.toggle("hidden", name !== "register");
    if (name === "login" && !captcha.login) refreshCaptcha("login");
    if (name === "register" && !captcha.register) refreshCaptcha("register");
  }
  tabs.forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  async function refreshCaptcha(which) {
    try {
      const data = await API.get("/api/auth/captcha");
      captcha[which] = data.captcha_id;
      if (which === "login") loginCaptchaImg.src = data.image;
      else regCaptchaImg.src = data.image;
    } catch (e) {
      showError("验证码加载失败，请检查服务是否启动");
    }
  }
  loginCaptchaImg.addEventListener("click", () => refreshCaptcha("login"));
  regCaptchaImg.addEventListener("click", () => refreshCaptcha("register"));

  function strength(pwd) {
    let score = 0;
    if (pwd.length >= 8) score++;
    if (/[A-Z]/.test(pwd)) score++;
    if (/[a-z]/.test(pwd)) score++;
    if (/\d/.test(pwd)) score++;
    if (/[^A-Za-z0-9]/.test(pwd)) score++;
    return Math.min(4, Math.max(0, score - 1));
  }

  regPwd.addEventListener("input", () => {
    const s = strength(regPwd.value);
    meter.className = "pwd-meter" + (s ? " s" + s : "");
    const labels = ["太弱", "较弱", "一般", "较强", "很强"];
    pwdHint.textContent = regPwd.value ? `密码强度：${labels[s]}（需 ≥8 位，含大小写字母、数字与特殊符号）` : "需 ≥8 位，同时包含大小写字母、数字与特殊符号";
  });

  function validatePhone(phone) {
    return /^1[3-9]\d{9}$/.test(phone);
  }

  function currentRole() {
    const checked = document.querySelector('input[name="regRole"]:checked');
    return checked ? checked.value : "employee";
  }

  function syncRole() {
    const isLeader = currentRole() === "leader";
    inviteField.classList.toggle("hidden", !isLeader);
    if (!isLeader) regInvite.value = "";
  }
  regRoles.forEach((el) => el.addEventListener("change", syncRole));
  syncRole();

  async function doLogin() {
    clearError();
    if (!validatePhone(loginPhone.value.trim())) return showError("请输入有效的 11 位手机号");
    if (!loginPwd.value) return showError("请输入密码");
    if (!loginCode.value.trim()) return showError("请输入图片验证码");
    loginBtn.disabled = true;
    loginBtn.textContent = "登录中…";
    try {
      const data = await API.post("/api/auth/login", {
        phone: loginPhone.value.trim(),
        password: loginPwd.value,
        captcha_id: captcha.login,
        captcha_code: loginCode.value.trim(),
      });
      API.save(data.token, data.user);
      location.href = "/";
    } catch (err) {
      showError(err.message);
      refreshCaptcha("login");
      loginCode.value = "";
      loginBtn.disabled = false;
      loginBtn.textContent = "登 录";
    }
  }

  async function doRegister() {
    clearError();
    const phone = regPhone.value.trim();
    const role = currentRole();
    if (!validatePhone(phone)) return showError("请输入有效的 11 位手机号");
    if (role === "leader" && !regInvite.value.trim()) return showError("请填写领导邀请码");
    if (regPwd.value !== regConfirm.value) return showError("两次输入的密码不一致");
    if (strength(regPwd.value) < 3 || regPwd.value.length < 8) {
      return showError("密码需至少 8 位，并包含大小写字母、数字和特殊符号");
    }
    if (!regCode.value.trim()) return showError("请输入图片验证码");
    regBtn.disabled = true;
    regBtn.textContent = "注册中…";
    try {
      const data = await API.post("/api/auth/register", {
        phone,
        password: regPwd.value,
        confirm_password: regConfirm.value,
        captcha_id: captcha.register,
        captcha_code: regCode.value.trim(),
        display_name: regName.value.trim(),
        role,
        invite_code: role === "leader" ? regInvite.value.trim() : "",
      });
      API.save(data.token, data.user);
      location.href = "/";
    } catch (err) {
      showError(err.message);
      refreshCaptcha("register");
      regCode.value = "";
      regBtn.disabled = false;
      regBtn.textContent = "注册并登录";
    }
  }

  loginBtn.addEventListener("click", doLogin);
  regBtn.addEventListener("click", doRegister);
  [loginPhone, loginPwd, loginCode].forEach((el) => el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doLogin();
  }));

  document.querySelectorAll("[data-fill]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      const [phone, pwd] = el.dataset.fill.split("|");
      loginPhone.value = phone;
      loginPwd.value = pwd;
      switchTab("login");
      API.toast("已填入演示账号，输入验证码后登录");
    });
  });

  switchTab("login");
})();
