#include "kitty_skins/process.hpp"

#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <string>
#include <system_error>
#include <vector>

#include <dirent.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

namespace kitty_skins {
namespace {

std::string drain(int fd) {
    std::string output;
    char chunk[4096];
    for (;;) {
        const ssize_t bytes = ::read(fd, chunk, sizeof(chunk));
        if (bytes > 0) {
            output.append(chunk, static_cast<size_t>(bytes));
            continue;
        }
        if (bytes == 0)
            break;
        if (errno == EINTR)
            continue;
        break;
    }
    return output;
}

}

ProcessResult run(std::span<const std::string> argv) {
    ProcessResult result{-1, {}};

    if (argv.empty()) {
        result.standardError = "no program to execute";
        return result;
    }

    std::vector<char*> arguments;
    arguments.reserve(argv.size() + 1);
    for (const std::string& argument : argv)
        arguments.push_back(const_cast<char*>(argument.c_str()));
    arguments.push_back(nullptr);

    int pipes[2] = {-1, -1};
    if (::pipe(pipes) != 0) {
        result.standardError = std::string("pipe: ") + std::strerror(errno);
        return result;
    }

    posix_spawn_file_actions_t actions;
    const int initResult = ::posix_spawn_file_actions_init(&actions);
    if (initResult != 0) {
        ::close(pipes[0]);
        ::close(pipes[1]);
        result.standardError = std::string("posix_spawn_file_actions_init: ") + std::strerror(initResult);
        return result;
    }
    const int dupResult = ::posix_spawn_file_actions_adddup2(&actions, pipes[1], STDERR_FILENO);
    ::posix_spawn_file_actions_addclose(&actions, pipes[0]);
    ::posix_spawn_file_actions_addclose(&actions, pipes[1]);
    if (dupResult != 0) {
        ::posix_spawn_file_actions_destroy(&actions);
        ::close(pipes[0]);
        ::close(pipes[1]);
        result.standardError = std::string("posix_spawn_file_actions_adddup2: ") + std::strerror(dupResult);
        return result;
    }

    pid_t pid = -1;
    const int spawnResult = ::posix_spawnp(&pid, arguments[0], &actions, nullptr, arguments.data(), environ);
    ::posix_spawn_file_actions_destroy(&actions);
    ::close(pipes[1]);

    if (spawnResult != 0) {
        ::close(pipes[0]);
        result.standardError = std::string("posix_spawnp: ") + std::strerror(spawnResult);
        return result;
    }

    result.standardError = drain(pipes[0]);
    ::close(pipes[0]);

    int status = 0;
    pid_t waited = -1;
    do {
        waited = ::waitpid(pid, &status, 0);
    } while (waited < 0 && errno == EINTR);

    if (waited < 0) {
        result.standardError += std::string("waitpid: ") + std::strerror(errno);
        return result;
    }

    if (WIFEXITED(status))
        result.exitCode = WEXITSTATUS(status);
    else if (WIFSIGNALED(status))
        result.exitCode = 128 + WTERMSIG(status);

    return result;
}

std::vector<pid_t> findKittyProcesses() {
    std::vector<pid_t> pids;

    DIR* proc = ::opendir("/proc");
    if (proc == nullptr)
        return pids;

    while (const dirent* entry = ::readdir(proc)) {
        const char* name = entry->d_name;
        if (name[0] < '0' || name[0] > '9')
            continue;

        char* end = nullptr;
        const long value = std::strtol(name, &end, 10);
        if (end == name || *end != '\0' || value <= 0)
            continue;

        std::error_code ec;
        const std::filesystem::path executable = std::filesystem::read_symlink(std::filesystem::path("/proc") / name / "exe", ec);
        if (ec)
            continue;

        if (executable.filename() == "kitty")
            pids.push_back(static_cast<pid_t>(value));
    }

    ::closedir(proc);

    std::sort(pids.begin(), pids.end());
    return pids;
}

void signalKittyProcesses(std::span<const pid_t> pids) {
    std::error_code failure;
    pid_t failedPid = -1;

    for (const pid_t pid : pids) {
        if (::kill(pid, SIGUSR1) == 0)
            continue;
        if (errno == ESRCH)
            continue;
        if (failedPid < 0) {
            failedPid = pid;
            failure = std::error_code(errno, std::generic_category());
        }
    }

    if (failedPid >= 0)
        throw std::system_error(failure, "SIGUSR1 to kitty process " + std::to_string(failedPid));
}

}
