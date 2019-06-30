package com.javaproject.demo;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
@MapperScan("com.javaproject.demo.mapper")
public class JavaProjectApplication {

    public static void main(String[] args) {

        SpringApplication.run(JavaProjectApplication.class, args);
    }

}
