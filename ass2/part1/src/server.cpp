#include <bits/stdc++.h>
#include <signal.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include "json_parser.h"
using namespace std;

vector<string> words;
bool running = true;

void signalHandler(int sig) {
    running = false;
}

void loadWords(const string& filename) {
    ifstream file(filename);
    if (!file.is_open()) {
        cerr << "Error: Cannot open " << filename << endl;
        exit(1);
    }
    
    string line;
    getline(file, line);
    
    stringstream ss(line);
    string word;
    while (getline(ss, word, ',')) {
        words.push_back(word);
    }
    file.close();
    cout << "Loaded " << words.size() << " words from " << filename << endl;
}

string processRequest(int offset, int k) {
    if (offset >= int(words.size())) {
        return "EOF\n";
    }
    
    string response;
    int count = 0;
    for (int i = offset; i < int(words.size()) && count < k; i++, count++) {
        if (count > 0) response += ",";
        response += words[i];
    }

    if (offset + k >= int(words.size())) {
        response += ",EOF";
    }
    response += "\n";
    return response;
}

int main(int argc, char* argv[]) {
    string configFile = "config.json";
    
    // Parse command line arguments
    for (int i = 1; i < argc; i++) {
        if (string(argv[i]) == "--config" && i + 1 < argc) {
            configFile = argv[i + 1];
            i++;
        }
    }
    
    auto cfg = parse_simple_json_file(configFile);
    if (cfg.empty()) {
        cerr << "Error: Cannot open or parse " << configFile << endl;
        return 1;
    }
    std::string serverIP = cfg["server_ip"];
    int serverPort = stoi(cfg["server_port"]);
    string filename = cfg["filename"];
    
    loadWords(filename);
    
    signal(SIGINT, signalHandler);
    signal(SIGTERM, signalHandler);
    
    int serverSocket = socket(AF_INET, SOCK_STREAM, 0);
    if (serverSocket < 0) {
        perror("Socket creation failed");
        return 1;
    }
    
    int opt = 1;
    setsockopt(serverSocket, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    
    // Set receive timeout for better signal handling
    struct timeval timeout;
    timeout.tv_sec = 1;
    timeout.tv_usec = 0;
    setsockopt(serverSocket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    
    sockaddr_in serverAddr;
    memset(&serverAddr, 0, sizeof(serverAddr));
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_addr.s_addr = INADDR_ANY;
    serverAddr.sin_port = htons(serverPort);
    
    if (bind(serverSocket, (sockaddr*)&serverAddr, sizeof(serverAddr)) < 0) {
        perror("Bind failed");
        close(serverSocket);
        return 1;
    }
    
    if (listen(serverSocket, 5) < 0) {
        perror("Listen failed");
        close(serverSocket);
        return 1;
    }
    
    cout << "Server listening on port " << serverPort << endl;
    
    while (running) {
        sockaddr_in clientAddr;
        socklen_t clientLen = sizeof(clientAddr);
        
        int clientSocket = accept(serverSocket, (sockaddr*)&clientAddr, &clientLen);
        if (clientSocket < 0) {
            if (running && errno != EAGAIN && errno != EWOULDBLOCK) {
                perror("Accept failed");
            }
            continue;
        }
        
        if (!running) {
            close(clientSocket);
            break;
        }
        
        char buffer[1024];
        while (true) {
            memset(buffer, 0, sizeof(buffer));
            int bytesRead = recv(clientSocket, buffer, sizeof(buffer) - 1, 0);
            
            if (bytesRead <= 0) break;
            
            string request(buffer);
            size_t commaPos = request.find(',');
            if (commaPos == string::npos) break;
            
            int offset = stoi(request.substr(0, commaPos));
            int k = stoi(request.substr(commaPos + 1));
            
            string response = processRequest(offset, k);
            send(clientSocket, response.c_str(), response.length(), 0);
        }
        
        close(clientSocket);
    }
    
    close(serverSocket);
    cout << "Server shutdown" << endl;
    return 0;
}
