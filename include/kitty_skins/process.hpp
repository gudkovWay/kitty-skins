#pragma once

#include <span>
#include <string>
#include <sys/types.h>
#include <vector>

namespace kitty_skins {

struct ProcessResult { int exitCode; std::string standardError; };
ProcessResult run(std::span<const std::string> argv);
std::vector<pid_t> findKittyProcesses();
void signalKittyProcesses(std::span<const pid_t> pids);

}
