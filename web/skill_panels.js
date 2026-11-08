/* 技能面板共享元数据（2026-11-09 Phase 1h）
 *
 * 目的：index.html 和 play.html 不再各写一套“幸运儿三选一”文案/选项。
 * 只放**纯数据**和取数函数，不碰 DOM、不碰 S.send：
 *   - 页面仍然负责自己的按钮/点击；
 *   - 以后加技能时，选项/提示只改这里。
 */
(function (global) {
  'use strict';

  var META = {
    lucky: {
      title: '幸运儿',
      hint: '三选一，用完就永远没有：① 查验（看阵营）② 守护（今夜挡狼刀）'
        + '③ 子弹（不主动杀人；被放逐/被狼刀出局时可开枪带走一人自证，被毒/被梦死不能开）。'
        + '也可以留到以后再用。',
      options: [
        { key: 'check', label: '① 查验', needsTarget: true },
        { key: 'guard', label: '② 守护', needsTarget: true },
        { key: 'bullet', label: '③ 子弹', needsTarget: false }
      ]
    },
    knight_judge: {
      title: '骑士审判',
      hint: '每局一次：审判狼人 → 对方翻牌出局；审判狼王 → 你和狼王一起出局，狼王不能带人；'
        + '审判好人 → 只公示对方是好人阵营，你不出局，但票权降为 0.5。无论结果，今天照常放逐投票。'
    },
    hunter_shoot: {
      title: '猎人开枪',
      hint: '你可以亮出猎人身份开枪带走一名玩家，也可以选择不开枪。'
    },
    lucky_shoot: {
      title: '幸运儿子弹',
      hint: '你选的是子弹：可以亮出幸运儿身份开枪带走一名玩家自证，也可以选择不开枪。'
    },
    wolf_king_take: {
      title: '狼王带人',
      hint: '你可以带走场上任意一名好人；不能带走狼队友，也可以选择不带人。'
    }
  };

  function meta(kind) {
    return META[String(kind || '')] || null;
  }

  function hint(kind) {
    var m = meta(kind);
    return m ? String(m.hint || '') : '';
  }

  function options(kind) {
    var m = meta(kind);
    if (!m || !Array.isArray(m.options)) return [];
    return m.options.map(function (o) {
      return { key: String(o.key), label: String(o.label), needsTarget: !!o.needsTarget };
    });
  }

  function option(kind, key) {
    var want = String(key || '');
    var hit = options(kind).filter(function (o) { return o.key === want; })[0];
    return hit || null;
  }

  // 纯选目标类技能：ask.kind -> payload 字段名。让 index/play 用同一张表。
  var SEAT_FIELD = {
    wolf_kill: 'target',
    seer_check: 'target',
    guard: 'target',
    dream: 'target',
    wolf_king_take: 'take',
    vote: 'vote',
    sheriff_vote: 'vote'
  };

  function seatField(kind) {
    return SEAT_FIELD[String(kind || '')] || null;
  }

  function curseTargets(ask) {
    var ex = (ask && ask.extra) || {};
    return Array.isArray(ex.curse_legal) ? ex.curse_legal.slice() : [];
  }

  function witchState(ask) {
    var ex = (ask && ask.extra) || {};
    var victim = (ex.victim === undefined ? null : ex.victim);
    var noVictim = (victim === null || victim === undefined);
    var hasHeal = ex.has_heal !== false;
    var hasPoison = ex.has_poison !== false;
    return {
      victim: victim,
      noVictim: noVictim,
      hasHeal: hasHeal,
      hasPoison: hasPoison,
      canSave: hasHeal && !noVictim,
      canPoison: hasPoison
    };
  }

  function renderWitch(container, ask, api) {
    var st = witchState(ask);
    var save = false;
    var poison = null;
    var saveRow = document.createElement('div');
    saveRow.className = 'seatpick';
    var yes = api.mkBtn(st.canSave ? ('救 ' + st.victim + '号（用解药）')
      : (st.noVictim ? '今晚没人被刀（不用解药）' : '解药已用完'),
      function () { if (!st.canSave) return; save = true; poison = null; paint(); });
    var no = api.mkBtn('不救', function () { save = false; paint(); });
    yes.disabled = !st.canSave;
    saveRow.appendChild(yes);
    saveRow.appendChild(no);
    container.appendChild(saveRow);

    var poisonRow = document.createElement('div');
    poisonRow.className = 'seatpick';
    if (st.canPoison) {
      var none = api.mkBtn('不用毒', function () { poison = null; paint(); });
      poisonRow.appendChild(none);
      (ask.legal || []).forEach(function (id) {
        poisonRow.appendChild(api.mkBtn('毒 ' + id + '号', function () {
          poison = id; save = false; paint();
        }));
      });
    }
    container.appendChild(poisonRow);

    var confirm = api.mkBtn('确认', function () {
      api.answer({ save: save, poison: poison, reason: '人类玩家自己决定的' });
      if (api.done) api.done();
    }, 'primary');
    container.appendChild(confirm);

    function paint() {
      yes.classList.toggle('on', save);
      no.classList.toggle('on', !save);
      Array.prototype.forEach.call(poisonRow.children, function (b) {
        var isNone = b.textContent === '不用毒';
        b.classList.toggle('on', isNone ? poison === null : b.textContent === '毒 ' + poison + '号');
      });
    }
    paint();
  }

  function renderLucky(container, ask, api) {
    var legal = (ask.legal || []).filter(function (x) { return x; });
    var pickedSkill = null;
    var pickedTarget = null;
    var hintEl = document.createElement('div');
    hintEl.className = 'hint';
    hintEl.textContent = hint('lucky');
    container.appendChild(hintEl);

    var skillRow = document.createElement('div');
    skillRow.className = 'seatpick';
    var seatRow = document.createElement('div');
    seatRow.className = 'seatpick';
    seatRow.style.display = 'none';
    container.appendChild(skillRow);
    container.appendChild(seatRow);

    options('lucky').forEach(function (o) {
      var b = api.mkBtn(o.label, function () {
        pickedSkill = o.key;
        Array.prototype.forEach.call(skillRow.children, function (x) { x.classList.remove('on'); });
        b.classList.add('on');
        seatRow.style.display = o.needsTarget ? 'flex' : 'none';
        if (!o.needsTarget) pickedTarget = null;
        paint();
      });
      skillRow.appendChild(b);
    });
    legal.forEach(function (id) {
      var b = api.mkBtn(id + '号', function () {
        pickedTarget = id;
        Array.prototype.forEach.call(seatRow.children, function (x) { x.classList.remove('on'); });
        b.classList.add('on');
        paint();
      });
      seatRow.appendChild(b);
    });

    var confirm = api.mkBtn('确认用掉这次机会', function () {
      var opt = option('lucky', pickedSkill);
      if (!opt) return;
      if (opt.needsTarget && pickedTarget === null) return;
      api.answer({ use: true, skill: pickedSkill,
                   target: opt.needsTarget ? pickedTarget : null,
                   reason: '人类玩家自己决定的' });
      if (api.done) api.done();
    }, 'primary');
    var later = api.mkBtn('留到以后（今晚不用）', function () {
      api.answer({ use: false, reason: '人类玩家留着不用' });
      if (api.done) api.done();
    });
    container.appendChild(confirm);
    container.appendChild(later);

    function paint() {
      var opt = option('lucky', pickedSkill);
      confirm.disabled = !opt || (opt.needsTarget && pickedTarget === null);
    }
    paint();
  }

  function renderKnight(container, ask, api) {
    var hintEl = document.createElement('div');
    hintEl.className = 'hint';
    hintEl.textContent = hint('knight_judge');
    container.appendChild(hintEl);
    var picked = null;
    var row = document.createElement('div');
    row.className = 'seatpick';
    (ask.legal || []).forEach(function (id) {
      var b = api.mkBtn('审判 ' + id + '号', function () {
        picked = id;
        Array.prototype.forEach.call(row.children, function (x) { x.classList.remove('on'); });
        b.classList.add('on');
      });
      row.appendChild(b);
    });
    container.appendChild(row);
    var confirm = api.mkBtn('发动审判', function () {
      if (picked === null) return;
      api.answer({ use: true, target: picked, reason: '人类玩家自己决定' });
      if (api.done) api.done();
    }, 'primary');
    var no = api.mkBtn('不发动（保留到以后）', function () {
      api.answer({ use: false, reason: '人类玩家选择不发动' });
      if (api.done) api.done();
    });
    container.appendChild(confirm);
    container.appendChild(no);
  }

  function renderShoot(container, ask, api) {
    var hintEl = document.createElement('div');
    hintEl.className = 'hint';
    hintEl.textContent = hint(ask.kind);
    container.appendChild(hintEl);
    var picked = null;
    var row = document.createElement('div');
    row.className = 'seatpick';
    (ask.legal || []).forEach(function (id) {
      var b = api.mkBtn(id + '号', function () {
        picked = id;
        Array.prototype.forEach.call(row.children, function (x) { x.classList.remove('on'); });
        b.classList.add('on');
      });
      row.appendChild(b);
    });
    var no = api.mkBtn('不开枪', function () {
      api.answer({ shoot: null, reason: '人类玩家选择不开枪' });
      if (api.done) api.done();
    });
    row.appendChild(no);
    container.appendChild(row);
    var confirm = api.mkBtn('确认开枪', function () {
      if (picked === null) return;
      api.answer({ shoot: picked, reason: '人类玩家自己决定的' });
      if (api.done) api.done();
    }, 'primary');
    container.appendChild(confirm);
  }

  function renderWolfKing(container, ask, api) {
    var hintEl = document.createElement('div');
    hintEl.className = 'hint';
    hintEl.textContent = hint('wolf_king_take');
    container.appendChild(hintEl);
    var row = document.createElement('div');
    row.className = 'seatpick';
    (ask.legal || []).forEach(function (id) {
      row.appendChild(api.mkBtn(id + '号', function () {
        api.answer({ take: id, reason: '人类玩家自己决定的' });
        if (api.done) api.done();
      }));
    });
    row.appendChild(api.mkBtn('不带人', function () {
      api.answer({ take: null, reason: '人类玩家选择不带人' });
      if (api.done) api.done();
    }));
    container.appendChild(row);
  }

  global.SkillPanels = {
    META: META, meta: meta, hint: hint, options: options, option: option,
    seatField: seatField, curseTargets: curseTargets, witchState: witchState,
    renderWitch: renderWitch, renderLucky: renderLucky,
    renderKnight: renderKnight, renderShoot: renderShoot, renderWolfKing: renderWolfKing
  };
})(window);
