(() => {
  'use strict';
  const samples = {
    memo: '次の打ち合わせでは、\n新しいアイデアを3つ持ち寄る',
    url: 'https://example.com/ideas',
    text: '小さなアイデアを、\n次の一歩につなげよう',
  };
  let selected = 'memo';
  const source = document.getElementById('demo-source');
  const destination = document.getElementById('demo-destination');
  const result = document.getElementById('demo-result');
  const send = document.getElementById('demo-send');
  source.style.whiteSpace = 'pre-line';
  document.querySelectorAll('[data-sample]').forEach(button => {
    button.addEventListener('click', () => {
      selected = button.dataset.sample;
      document.querySelectorAll('[data-sample]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      source.textContent = samples[selected];
      destination.replaceChildren(Object.assign(document.createElement('span'), {className: 'placeholder', textContent: 'ここに、コピーした文字が届きます'}));
      result.textContent = 'iPhone側の「PCへ送る」を押してください';
      result.className = 'demo-result';
      send.querySelector('span').textContent = 'タップして試す';
    });
  });
  send.addEventListener('click', () => {
    destination.textContent = samples[selected];
    destination.classList.remove('sent-flash');
    void destination.offsetWidth;
    destination.classList.add('sent-flash');
    result.textContent = '送信完了 · PCで貼り付けて使えます';
    result.className = 'demo-result success';
    send.querySelector('span').textContent = 'もう一度試す';
  });
  const links = [...document.querySelectorAll('.nav-link')];
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      const active = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!active) return;
      links.forEach(link => {
        const matches = link.hash === '#' + active.target.id;
        link.classList.toggle('active', matches);
        if (matches) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }, {rootMargin: '-10% 0px -55% 0px', threshold: [0, .2, .5]});
    document.querySelectorAll('#top, #experience, #how-it-works, #setup, #faq').forEach(section => observer.observe(section));
  }
})();
