#include <bits/stdc++.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include "json_parser.h"
using namespace std;
using namespace chrono;

map<string, int> wordCount;

void countWords(const string& wordsLine) {
    stringstream ss(wordsLine);
    string word;
    while (getline(ss, word, ',')) {
        if (word != "EOF" && !word.empty()) {
            wordCount[word]++;
        }
    }
}

int main(int argc, char* argv[]) {
    string configFile = "config.json";
    int kOverride = -1;
    bool quiet = false;
    
    // Parse command line arguments
    for (int i = 1; i < argc; i++) {
        if (string(argv[i]) == "--config" && i + 1 < argc) {
            configFile = argv[i + 1];
            i++;
        } else if (string(argv[i]) == "--k" && i + 1 < argc) {
            kOverride = stoi(argv[i + 1]);
            i++;
        } else if (string(argv[i]) == "--quiet") {
            quiet = true;
        }
    }
    
    auto cfg = parse_simple_json_file(configFile);
    if (cfg.empty()) {
        cerr << "Error: Cannot open or parse " << configFile << endl;
        return 1;
    }
    string serverIP = cfg["server_ip"];
    int serverPort = stoi(cfg["server_port"]);
    int k = (kOverride != -1) ? kOverride : stoi(cfg["k"]);
    int p = stoi(cfg["p"]);
    int numIterations = stoi(cfg["num_iterations"]);
    
    auto startTime = high_resolution_clock::now();
    
    for (int iter = 0; iter < numIterations; iter++) {
        int clientSocket = socket(AF_INET, SOCK_STREAM, 0);
        if (clientSocket < 0) {
            perror("Socket creation failed");
            return 1;
        }
        
        sockaddr_in serverAddr;
        memset(&serverAddr, 0, sizeof(serverAddr));
        serverAddr.sin_family = AF_INET;
        serverAddr.sin_port = htons(serverPort);
        inet_pton(AF_INET, serverIP.c_str(), &serverAddr.sin_addr);
        
        if (connect(clientSocket, (sockaddr*)&serverAddr, sizeof(serverAddr)) < 0) {
            perror("Connection failed");
            close(clientSocket);
            return 1;
        }
        
        int offset = p;
        bool done = false;
        
        while (!done) {
            string request = to_string(offset) + "," + to_string(k) + "\n";
            send(clientSocket, request.c_str(), request.length(), 0);
            
            char buffer[4096];
            memset(buffer, 0, sizeof(buffer));
            int bytesRead = recv(clientSocket, buffer, sizeof(buffer) - 1, 0);
            
            if (bytesRead <= 0) break;
            
            string response(buffer);
            response = response.substr(0, response.find('\n'));
            
            if (response == "EOF") {
                done = true;
            } else {
                countWords(response);
                if (response.find("EOF") != string::npos) {
                    done = true;
                }
                offset += k;
            }
        }
        
        close(clientSocket);
    }
    
    auto endTime = high_resolution_clock::now();
    auto duration = duration_cast<milliseconds>(endTime - startTime);
    
    if (!quiet) {
        for (const auto& pair : wordCount) {
            cout << pair.first << ", " << pair.second << endl;
        }
    }
    
    cout << "ELAPSED_MS:" << duration.count() << endl;
    
    return 0;
}
