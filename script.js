const messages = [
    "are u sure",
    "like super sure",
    "like 100% sure",
    "pookie pls what is ts",
    "PLEEEEEEEEJ",
    "if u say no i will not baat to u.",
    "okay ill tell oreo not to talk to u too",
    "i'll be so so sad :(",
    "phine i will stop asking",
    "HAH u funny jit imma keep asking till u say yes",
    "what other huzz asked u first lil bro",
    "js say u hate me atp",
    "so you're sure",
];

let messageIndex = 0;

function handleNoClick() {
    const noButton = document.querySelector('.no-button');
    const yesButton = document.querySelector('.yes-button');
    noButton.textContent = messages[messageIndex];
    messageIndex = (messageIndex + 1) % messages.length;
    const currentSize = parseFloat(window.getComputedStyle(yesButton).fontSize);
    yesButton.style.fontSize = `${currentSize * 1.5}px`;
}

function handleYesClick() {
    window.location.href = "yes_page.html";
}
