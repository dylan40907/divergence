const quantity = document.querySelector('#quantity');
const total = document.querySelector('#total');
const status = document.querySelector('#coupon-status');
let totalCents = 2500;
let couponApplied = false;

function render() {
  total.dataset.cents = String(totalCents);
  total.textContent = `$${(totalCents / 100).toFixed(2)}`;
  status.textContent = couponApplied ? 'WELCOME10 applied' : '10% off your basket';
}

quantity.addEventListener('input', () => {
  const count = Math.max(1, Math.min(10, Number(quantity.value) || 1));
  totalCents = count * 2500;
  if (couponApplied) totalCents = Math.round(totalCents * 0.9);
  render();
});

function applyCoupon() {
  if (couponApplied) return;
  totalCents = Math.round(totalCents * 0.9);
  couponApplied = true;
  render();
}

document.querySelector('#apply-coupon').addEventListener('click', applyCoupon);
