"use strict";

const studentSearch = document.getElementById("student_search");
const studentOptions = document.getElementById("studentOptions");
const studentCount = document.getElementById("studentCount");
const studentCombobox = document.getElementById("studentCombobox");
const pagination = document.getElementById("searchPagination");
const previousPage = document.getElementById("previousPage");
const nextPage = document.getElementById("nextPage");
const pageLabel = document.getElementById("pageLabel");
const selectionPanel = document.getElementById("selectedStudents");
const selectionCount = document.getElementById("selectionCount");
const selectionList = document.getElementById("selectedStudentList");
const selectionInputs = document.getElementById("selectedStudentInputs");
const arrivalForm = document.getElementById("arrivalForm");
const submitButton = document.getElementById("submitButton");

// Keep selections separate from the current query and result page.
const selectedStudents = new Map();
let matchingStudents = [];
let currentPage = 1;
let hasMore = false;
let loading = false;
let activeIndex = -1;
let searchTimer = null;
let requestVersion = 0;

function closeDropdown() {
    studentOptions.classList.remove("open");
    studentSearch.setAttribute("aria-expanded", "false");
    studentSearch.removeAttribute("aria-activedescendant");
    activeIndex = -1;
}

function openDropdown() {
    studentOptions.classList.add("open");
    studentSearch.setAttribute("aria-expanded", "true");
}

function updateResultSelections() {
    studentOptions.querySelectorAll('[role="option"]').forEach((option, index) => {
        const selected = selectedStudents.has(String(matchingStudents[index].student_no));
        option.setAttribute("aria-selected", String(selected));
        option.querySelector(".student-option-check").textContent = selected ? "✓" : "";
    });
}

function renderSelection() {
    selectionList.replaceChildren();
    selectionInputs.replaceChildren();
    selectionPanel.classList.toggle("visible", selectedStudents.size > 0);
    selectionCount.textContent = `${selectedStudents.size} student(s) selected`;
    submitButton.textContent = selectedStudents.size > 0
        ? `✓ Record Late Arrivals (${selectedStudents.size})`
        : "✓ Record Late Arrivals";

    selectedStudents.forEach((student, studentNo) => {
        const item = document.createElement("li");
        const details = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = student.student_name;
        const grade = document.createElement("div");
        grade.className = "student-option-class";
        grade.textContent = `Grade ${student.grade}-${student.section} · No. ${studentNo}`;
        details.append(name, grade);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove-student";
        remove.textContent = "Remove";
        remove.setAttribute("aria-label", `Remove ${student.student_name}, No. ${studentNo}`);
        remove.addEventListener("click", () => {
            selectedStudents.delete(studentNo);
            renderSelection();
            updateResultSelections();
            studentSearch.focus();
        });
        item.append(details, remove);
        selectionList.appendChild(item);

        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "student_no";
        input.value = studentNo;
        selectionInputs.appendChild(input);
    });
    studentSearch.setCustomValidity("");
}

function toggleStudent(student) {
    const studentNo = String(student.student_no);
    if (selectedStudents.has(studentNo)) {
        selectedStudents.delete(studentNo);
    } else {
        selectedStudents.set(studentNo, student);
    }
    renderSelection();
    updateResultSelections();
}

function renderStudents() {
    studentOptions.replaceChildren();
    activeIndex = -1;
    studentSearch.removeAttribute("aria-activedescendant");

    if (matchingStudents.length === 0) {
        const empty = document.createElement("div");
        empty.className = "student-option empty";
        empty.textContent = "No matching students found";
        studentOptions.appendChild(empty);
    }

    matchingStudents.forEach((student, index) => {
        const option = document.createElement("div");
        option.className = "student-option";
        option.id = `student-option-${index}`;
        option.setAttribute("role", "option");

        const check = document.createElement("span");
        check.className = "student-option-check";
        check.setAttribute("aria-hidden", "true");
        const details = document.createElement("div");
        const name = document.createElement("div");
        name.className = "student-option-name";
        name.textContent = student.student_name;
        const grade = document.createElement("div");
        grade.className = "student-option-class";
        grade.textContent = `Grade ${student.grade}-${student.section} · No. ${student.student_no}`;
        details.append(name, grade);
        option.append(check, details);
        option.addEventListener("mousedown", event => event.preventDefault());
        option.addEventListener("click", () => toggleStudent(student));
        studentOptions.appendChild(option);
    });

    updateResultSelections();
    openDropdown();
}

async function searchStudents(query, page = 1) {
    clearTimeout(searchTimer);
    const version = ++requestVersion;
    query = query.trim();
    matchingStudents = [];
    studentOptions.replaceChildren();
    closeDropdown();
    pagination.hidden = true;
    loading = false;

    if (query.length < 2) {
        studentCount.textContent = "Enter at least 2 characters.";
        return;
    }

    loading = true;
    studentCount.textContent = "Searching students...";
    try {
        const response = await fetch(
            `/api/student-search?q=${encodeURIComponent(query)}&page=${page}`
        );
        if (!response.ok) throw new Error("Search failed");
        const result = await response.json();
        if (version !== requestVersion) return;

        matchingStudents = result.students;
        currentPage = result.page;
        hasMore = result.has_more;
        loading = false;
        studentCount.textContent = `${matchingStudents.length} match(es) on page ${currentPage}.`;
        pageLabel.textContent = `Page ${currentPage}`;
        previousPage.disabled = currentPage <= 1;
        nextPage.disabled = !hasMore;
        pagination.hidden = currentPage <= 1 && !hasMore;
        renderStudents();
    } catch (error) {
        if (version !== requestVersion) return;
        loading = false;
        studentCount.textContent = "Unable to search students. Try again.";
        closeDropdown();
    }
}

studentSearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    ++requestVersion;
    studentSearch.setCustomValidity("");
    matchingStudents = [];
    loading = false;
    currentPage = 1;
    pagination.hidden = true;
    closeDropdown();
    const query = studentSearch.value;
    studentCount.textContent = query.trim().length < 2
        ? "Enter at least 2 characters." : "Searching students...";
    searchTimer = setTimeout(() => searchStudents(query), 250);
});

studentSearch.addEventListener("focus", () => {
    if (matchingStudents.length > 0) {
        renderStudents();
    } else if (studentSearch.value.trim().length >= 2 && !loading) {
        searchStudents(studentSearch.value);
    }
});

document.getElementById("dropdownArrow").addEventListener("click", () => {
    if (studentOptions.classList.contains("open")) {
        closeDropdown();
    } else {
        studentSearch.focus();
        if (matchingStudents.length > 0) renderStudents();
    }
});

previousPage.addEventListener("click", () => {
    if (!loading && currentPage > 1) searchStudents(studentSearch.value, currentPage - 1);
});
nextPage.addEventListener("click", () => {
    if (!loading && hasMore) searchStudents(studentSearch.value, currentPage + 1);
});

studentSearch.addEventListener("keydown", event => {
    if (event.key === "Escape") {
        closeDropdown();
        return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!studentOptions.classList.contains("open") && matchingStudents.length > 0) {
            renderStudents();
        }
        const options = Array.from(studentOptions.querySelectorAll('[role="option"]'));
        if (!options.length) return;
        if (event.key === "ArrowDown") {
            activeIndex = (activeIndex + 1) % options.length;
        } else {
            activeIndex = activeIndex <= 0 ? options.length - 1 : activeIndex - 1;
        }
        options.forEach((option, index) => {
            option.classList.toggle("active", index === activeIndex);
        });
        studentSearch.setAttribute("aria-activedescendant", options[activeIndex].id);
        options[activeIndex].scrollIntoView({block: "nearest"});
    }
    if (event.key === "Enter" && studentOptions.classList.contains("open")) {
        event.preventDefault();
        if (activeIndex >= 0) toggleStudent(matchingStudents[activeIndex]);
    }
});

document.addEventListener("click", event => {
    if (!studentCombobox.contains(event.target) && !pagination.contains(event.target)) {
        closeDropdown();
    }
});

arrivalForm.addEventListener("submit", event => {
    if (submitButton.disabled) {
        event.preventDefault();
        return;
    }
    if (!selectedStudents.size) {
        event.preventDefault();
        studentSearch.setCustomValidity("Please select at least one student from the suggestions.");
        studentSearch.reportValidity();
        studentSearch.focus();
        return;
    }
    studentSearch.setCustomValidity("");
    submitButton.disabled = true;
    submitButton.textContent = "Recording...";
});

arrivalForm.addEventListener("reset", () => {
    clearTimeout(searchTimer);
    ++requestVersion;
    selectedStudents.clear();
    matchingStudents = [];
    currentPage = 1;
    hasMore = false;
    loading = false;
    renderSelection();
    studentOptions.replaceChildren();
    studentCount.textContent = "";
    pagination.hidden = true;
    submitButton.disabled = false;
    closeDropdown();
});
