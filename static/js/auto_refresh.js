/**
 * 全站自动刷新：前台按间隔轮询；后台暂停；切回若数据过期则立即补刷。
 * 浏览器会大幅节流后台 setInterval，仅靠定时器挂久必会过期。
 */
(function (global) {
  function startAutoRefresh(opts) {
    const intervalMs = Math.max(5000, Number(opts.intervalMs) || 180000);
    const staleAfterMs = Math.max(1000, Number(opts.staleAfterMs) || intervalMs);
    const refresh = opts.refresh;
    if (typeof refresh !== "function") {
      throw new Error("AthenaAutoRefresh: refresh required");
    }

    let timer = null;
    let inFlight = false;
    let lastOkAt = Date.now();
    let stopped = false;

    async function run(reason) {
      if (stopped || document.hidden || inFlight) return false;
      inFlight = true;
      try {
        await refresh(reason || "interval");
        lastOkAt = Date.now();
        if (typeof opts.onSuccess === "function") {
          opts.onSuccess({ lastOkAt: lastOkAt, reason: reason || "interval" });
        }
        return true;
      } catch (err) {
        if (typeof opts.onError === "function") opts.onError(err, reason || "interval");
        return false;
      } finally {
        inFlight = false;
      }
    }

    function clearTimer() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    }

    function arm() {
      clearTimer();
      if (stopped || document.hidden) return;
      timer = setInterval(function () {
        run("interval");
      }, intervalMs);
    }

    function onVisibility() {
      if (document.hidden) {
        clearTimer();
        return;
      }
      if (Date.now() - lastOkAt >= staleAfterMs) {
        run("visible");
      }
      arm();
    }

    function onPageShow(ev) {
      if (!ev.persisted || document.hidden) return;
      if (Date.now() - lastOkAt >= staleAfterMs) {
        run("pageshow");
      }
      arm();
    }

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pageshow", onPageShow);
    arm();

    return {
      refreshNow: function (reason) {
        return run(reason || "manual");
      },
      markFresh: function () {
        lastOkAt = Date.now();
      },
      stop: function () {
        stopped = true;
        clearTimer();
        document.removeEventListener("visibilitychange", onVisibility);
        window.removeEventListener("pageshow", onPageShow);
      },
      get lastOkAt() {
        return lastOkAt;
      },
      get intervalMs() {
        return intervalMs;
      },
    };
  }

  global.AthenaAutoRefresh = { start: startAutoRefresh };
})(typeof window !== "undefined" ? window : globalThis);
