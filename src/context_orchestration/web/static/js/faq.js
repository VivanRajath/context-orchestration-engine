/* The questions, folded until they are asked.

   One listener on the list rather than one per question: the answers are in
   the markup, so there is nothing to bind late and nothing to rebind. Items
   open independently, because two questions being read together is a normal
   thing to want and closing one to open another is not. */

export function initFaq() {
  const list = document.querySelector(".faq");
  if (!list) { return; }

  list.addEventListener("click", function (ev) {
    const button = ev.target.closest(".faq-q");
    if (!button || !list.contains(button)) { return; }

    const item = button.closest(".faq-item");
    const answer = item && item.querySelector(".ans");
    if (!answer) { return; }

    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", open ? "true" : "false");
    item.classList.toggle("open", open);
    // hidden, not a display rule: it is what a screen reader and the browser's
    // own find-in-page both already understand.
    answer.hidden = !open;
  });
}
